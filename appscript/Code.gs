/**
 * MDS CAB RECOMMENDER — Metabase HTTP proxy  (STANDALONE Apps Script project)
 * ===========================================================================
 * Turns an authenticated Metabase call into a plain HTTP endpoint the sync job
 * can hit. Nothing else lives here — no Sheet, no schedule, no state. The
 * recommender service owns all of that; this file is a dumb authenticated pipe.
 *
 * SETUP (once, ~5 minutes)
 * -----------------------
 * 1. Go to https://script.google.com  →  New project. Name it "MDS Reco Metabase Proxy".
 * 2. Delete the stub Code.gs contents, paste this whole file in.
 * 3. Add the shared auth library:
 *      Editor sidebar → Libraries (+) → paste Script ID:
 *        1mX6q0A-e_ULxABD9khvnt_vDgYYKcktdPPqsxRwY-IcKwTC9uekcia0Y
 *      → Look up → pick the latest version → set Identifier to exactly: MISLib → Add
 * 4. Set API_TOKEN below to a long random string (e.g. run: openssl rand -hex 24).
 * 5. Deploy → New deployment → type "Web app"
 *      Execute as:  Me
 *      Who has access:  Anyone
 *    → Deploy → authorise → copy the URL ending in /exec.  THAT is the "/exec URL".
 * 6. Smoke test in a terminal:
 *      curl -sL "<EXEC_URL>?route=ping&token=<API_TOKEN>"
 *      → should print:  ok 2026-...
 *
 * REDEPLOYING AFTER AN EDIT
 *   Deploy → Manage deployments → pencil icon → Version: "New version" → Deploy.
 *   Use "New version" on the EXISTING deployment so the /exec URL stays the same.
 *   ("New deployment" mints a different URL and the sync job breaks.)
 *
 * ROUTES  (every route needs &token=<API_TOKEN>)
 *   ?route=ping
 *   ?route=databases                       → JSON, to find the `firstcut` database id
 *   ?route=card&id=<cardId>&start=YYYY-MM-DD&end=YYYY-MM-DD[&format=csv|json]
 *   ?route=sql&db=<dbId>&q=<url-encoded SQL>     (short ad-hoc queries)
 *   POST {route:"sql", token:"...", db:<id>, q:"<SQL>", format:"csv"}   (long queries)
 *
 * LIMITS — Apps Script kills a script at ~6 min and a UrlFetch at ~60 s, and very
 * large responses fail. Pull big ranges in chunks from the client side; a single
 * day of Hyderabad trips (~6k rows) is well inside the limits.
 */

// ─────────────────────────────── CONFIG ───────────────────────────────
var MB_HOST   = 'https://analytics.moveinsync.com';   // no trailing slash
var API_TOKEN = 'CHANGE_ME_TO_A_LONG_RANDOM_STRING';
var MB_API_KEY = '';   // optional: if you ever get a permanent Metabase API key,
                       // paste it here and it takes precedence over MISLib.
// ──────────────────────────────────────────────────────────────────────

var cache = CacheService.getScriptCache();


// ════════════════════════════════ ROUTER ══════════════════════════════

function doGet(e) {
  var p = (e && e.parameter) || {};
  if (p.token !== API_TOKEN) return _json({ error: 'bad or missing token' });
  try {
    switch (p.route) {
      case 'ping':
        return _text('ok ' + new Date().toISOString());

      case 'databases':
        return _json(_mbJson('/api/database'));

      case 'schema':
        if (!p.db || !p.table) return _json({ error: 'need db= and table=' });
        return _text(_dataset(Number(p.db),
          "select column_name, data_type from information_schema.columns " +
          "where table_name = '" + String(p.table).replace(/'/g, "''") + "' " +
          "order by ordinal_position", 'csv'));

      case 'card':
        if (!p.id) return _json({ error: 'need id=' });
        var params = (p.start && p.end) ? { start_date: p.start, end_date: p.end } : null;
        var fmt = (p.format === 'json') ? 'json' : 'csv';
        return _text(_card(Number(p.id), params, fmt), fmt);

      case 'sql':
        if (!p.db || !p.q) return _json({ error: 'need db= and q=' });
        return _text(_dataset(Number(p.db), p.q, 'csv'));

      default:
        return _json({ error: 'unknown route: ' + (p.route || '(none)') });
    }
  } catch (err) {
    return _json({ error: String((err && err.message) || err) });
  }
}

/** POST is only for SQL too long to fit in a query string. */
function doPost(e) {
  var b = {};
  try { b = JSON.parse((e && e.postData && e.postData.contents) || '{}'); } catch (_) {}
  if (b.token !== API_TOKEN) return _json({ error: 'bad or missing token' });
  try {
    if (b.route !== 'sql') return _json({ error: 'only route:"sql" is supported on POST' });
    if (!b.db || !b.q)     return _json({ error: 'need db and q' });
    var fmt = (b.format === 'json') ? 'json' : 'csv';
    return _text(_dataset(Number(b.db), b.q, fmt), fmt);
  } catch (err) {
    return _json({ error: String((err && err.message) || err) });
  }
}


// ═══════════════════════════ METABASE CALLS ═══════════════════════════

function _mbHeaders() {
  if (MB_API_KEY) return { 'X-API-KEY': MB_API_KEY };
  return { 'X-Metabase-Session': MISLib.authenticateDB() };
}

/** Saved question → CSV/JSON. Retries once if the cached session went stale. */
function _card(cardId, dateParams, fmt, _retried) {
  var payload = {};
  if (dateParams) {
    payload.parameters = JSON.stringify([
      { type: 'date/single', target: ['variable', ['template-tag', 'start_date']], value: dateParams.start_date },
      { type: 'date/single', target: ['variable', ['template-tag', 'end_date']],   value: dateParams.end_date }
    ]);
  }
  var resp = UrlFetchApp.fetch(MB_HOST + '/api/card/' + cardId + '/query/' + fmt, {
    method: 'post', headers: _mbHeaders(), payload: payload, muteHttpExceptions: true
  });
  var code = resp.getResponseCode();
  if ((code === 401 || code === 403) && !_retried) {
    cache.remove('DBSessionID');
    return _card(cardId, dateParams, fmt, true);
  }
  if (code === 401 || code === 403) {
    throw new Error('card ' + cardId + ' → HTTP ' + code + ' after session refresh. ' +
      'The mis-one-devs service account probably cannot SEE this card — move the question ' +
      'out of your personal collection into a shared one (⋮ → Move).');
  }
  if (code >= 300) throw new Error('card ' + cardId + ' → HTTP ' + code + ': ' +
                                   resp.getContentText().slice(0, 300));
  return resp.getContentText();
}

/** Ad-hoc native SQL → CSV/JSON. Form-encoded first, JSON body as fallback. */
function _dataset(dbId, sql, fmt, _retried) {
  var query = { database: dbId, type: 'native', native: { query: sql } };
  var resp = UrlFetchApp.fetch(MB_HOST + '/api/dataset/' + fmt, {
    method: 'post', headers: _mbHeaders(),
    payload: { query: JSON.stringify(query) }, muteHttpExceptions: true
  });
  var code = resp.getResponseCode();
  if ((code === 401 || code === 403) && !_retried) {
    cache.remove('DBSessionID');
    return _dataset(dbId, sql, fmt, true);
  }
  if (code === 400 || code === 415) {          // some Metabase builds want a JSON body
    var alt = UrlFetchApp.fetch(MB_HOST + '/api/dataset/' + fmt, {
      method: 'post', headers: _mbHeaders(),
      contentType: 'application/json', payload: JSON.stringify(query),
      muteHttpExceptions: true
    });
    if (alt.getResponseCode() < 300) return alt.getContentText();
    throw new Error('/api/dataset/' + fmt + ' → ' + code + ' (form) / ' +
                    alt.getResponseCode() + ' (json): ' + alt.getContentText().slice(0, 300));
  }
  if (code >= 300) throw new Error('/api/dataset/' + fmt + ' → HTTP ' + code + ': ' +
                                   resp.getContentText().slice(0, 300));
  return resp.getContentText();
}

function _mbJson(path, _retried) {
  var resp = UrlFetchApp.fetch(MB_HOST + path, {
    method: 'get', headers: _mbHeaders(), muteHttpExceptions: true
  });
  var code = resp.getResponseCode();
  if ((code === 401 || code === 403) && !_retried) {
    cache.remove('DBSessionID');
    return _mbJson(path, true);
  }
  if (code >= 300) throw new Error('GET ' + path + ' → HTTP ' + code);
  return JSON.parse(resp.getContentText());
}


// ═══════════════════════════════ HELPERS ══════════════════════════════

function _json(o) {
  return ContentService.createTextOutput(JSON.stringify(o))
                       .setMimeType(ContentService.MimeType.JSON);
}
function _text(s, kind) {
  return ContentService.createTextOutput(s).setMimeType(
    kind === 'json' ? ContentService.MimeType.JSON : ContentService.MimeType.TEXT);
}


// ══════════════ RUN ONCE FROM THE EDITOR TO VERIFY SETUP ══════════════
/** Confirms the MISLib library works and prints the `firstcut` database id. */
function checkSetup() {
  var s = MISLib.authenticateDB();
  Logger.log('MISLib session OK (length ' + String(s).length + ')');
  var dbs = _mbJson('/api/database');
  (dbs.data || dbs).forEach(function (d) {
    Logger.log(d.id + '  ' + d.name + '  (' + d.engine + ')');
  });
  Logger.log('--> use the id next to `firstcut` as the db= parameter');
}
