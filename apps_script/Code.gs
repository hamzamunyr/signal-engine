/**
 * Signal Engine -> Google Sheet receiver.
 * Paste into Extensions > Apps Script of the lead sheet, set TOKEN in Project Settings > Script properties
 * (key SHEET_TOKEN), then Deploy > New deployment > Web app (Execute as: Me, Who has access: Anyone).
 * The engine POSTs {token, client, rows:[...]}. One row per company (key); new evidence is merged into
 * the existing row's Timeline and the higher priority wins. Your own columns (Status, Notes) are never touched.
 */
var SHEET_NAME = 'Leads';
var HEADERS = ['Key', 'Company', 'Domain', 'Priority', 'Queue', 'Trigger', 'Signals', 'Intent', 'ICP fit',
  'Size', 'Evidence', 'Angle', 'Contact', 'Decision maker', 'Source', 'Link', 'Signal date', 'Timeline',
  'First seen', 'Last updated', 'Status', 'Notes'];
var FIELD = {key: 1, company: 2, domain: 3, priority: 4, queue: 5, trigger: 6, signals: 7, intent: 8, icp_fit: 9,
  size: 10, evidence: 11, angle: 12, contact: 13, dm_role: 14, source: 15, link: 16, signal_date: 17};
var TIMELINE = 18, FIRST = 19, UPDATED = 20, STATUS = 21;

function sheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) { sh = ss.getSheets()[0]; sh.setName(SHEET_NAME); }
  if (sh.getLastRow() === 0) {
    sh.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}

function json_(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}

function doGet() {
  return json_({ok: true, sheet: SHEET_NAME});
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var body = JSON.parse(e.postData.contents);
    var token = PropertiesService.getScriptProperties().getProperty('SHEET_TOKEN');
    if (!token || body.token !== token) return json_({ok: false, error: 'bad token'});
    var sh = sheet_();
    var last = sh.getLastRow();
    var keys = last > 1 ? sh.getRange(2, 1, last - 1, 1).getValues().map(function (r) { return String(r[0]); }) : [];
    var now = new Date();
    var added = 0, merged = 0;
    (body.rows || []).forEach(function (row) {
      var i = keys.indexOf(String(row.key));
      if (i === -1) {
        var vals = new Array(HEADERS.length).fill('');
        Object.keys(FIELD).forEach(function (f) { vals[FIELD[f] - 1] = row[f] === undefined ? '' : row[f]; });
        vals[TIMELINE - 1] = row.timeline_entry || '';
        vals[FIRST - 1] = now;
        vals[UPDATED - 1] = now;
        vals[STATUS - 1] = 'new';
        sh.appendRow(vals);
        keys.push(String(row.key));
        added++;
      } else {
        var r = i + 2;
        var range = sh.getRange(r, 1, 1, HEADERS.length);
        var cur = range.getValues()[0];
        var tl = String(cur[TIMELINE - 1] || '');
        if (row.timeline_entry && tl.indexOf(row.timeline_entry) === -1) tl = tl ? tl + '\n' + row.timeline_entry : row.timeline_entry;
        cur[TIMELINE - 1] = tl;
        cur[UPDATED - 1] = now;
        if (Number(row.priority) >= Number(cur[FIELD.priority - 1] || 0)) {
          Object.keys(FIELD).forEach(function (f) {
            if (f !== 'key' && row[f] !== '' && row[f] !== undefined) cur[FIELD[f] - 1] = row[f];
          });
        }
        range.setValues([cur]);
        merged++;
      }
    });
    if (added) {
      var n = sh.getLastRow() - 1;
      if (n > 1) sh.getRange(2, 1, n, HEADERS.length).sort({column: FIELD.priority, ascending: false});
    }
    return json_({ok: true, added: added, merged: merged});
  } catch (err) {
    return json_({ok: false, error: String(err)});
  } finally {
    lock.releaseLock();
  }
}
