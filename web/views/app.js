(function () {
  'use strict';

  var HI_DAYS = ['रविवार', 'सोमवार', 'मंगलवार', 'बुधवार', 'गुरुवार', 'शुक्रवार', 'शनिवार'];
  var EN_DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  var HI_MONTHS = ['जनवरी', 'फ़रवरी', 'मार्च', 'अप्रैल', 'मई', 'जून', 'जुलाई', 'अगस्त', 'सितंबर', 'अक्टूबर', 'नवंबर', 'दिसंबर'];
  var EN_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  var STAGES = {
    reading: ['Reading the files', 1],
    predicting: ['Predicting each item', 2],
    testing: ['Testing against last week', 3],
    wording: ['Wording the note', 4]
  };
  var LANG_ATTR = { Hindi: 'hi', English: 'en', Hinglish: 'hi-Latn' };
  var TICKS = 25;
  var POLL_MS = 1500;
  var TIMEOUT_MS = 20000; // each request gives up after this
  var POLL_GIVE_UP_MS = 4 * 60 * 1000; // a check that runs longer than this is given up on
  var NS = 'http://www.w3.org/2000/svg';

  var $ = function (id) { return document.getElementById(id); };
  var reduced = false;
  try { reduced = matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { /* old browser */ }

  var state = {
    config: null,
    language: 'Hindi',
    busy: false,
    questions: [],
    lastSample: false,
    resultSample: false,
    jobId: null,
    result: null,
    lines: [],
    linesLanguage: null,
    linesByAi: [],
    wordedByAi: 0,
    gemmaLimited: false,
    quotaOut: false,
    printing: false
  };

  // ---------- small helpers ----------

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === 'text') node.textContent = value;
        else if (key === 'class') node.className = value;
        else node.setAttribute(key, value === true ? '' : value);
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    });
    return node;
  }

  function icon(paths, size) {
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + (size || 16) + ' ' + (size || 16));
    svg.setAttribute('aria-hidden', 'true');
    paths.forEach(function (d) {
      var p = document.createElementNS(NS, 'path');
      p.setAttribute('d', d);
      svg.appendChild(p);
    });
    return svg;
  }
  // a pen nib for lines Gemma worded, a ruled line for the plain sentence
  function penIcon() { return icon(['M3 13l2-6 6-6 4 4-6 6z', 'M3 13l4-4']); }
  function plainIcon() { return icon(['M2 5h12', 'M2 11h8']); }
  function plusIcon() { return icon(['M9 2v14M2 9h14'], 18); }

  function parseDay(text) {
    var parts = String(text).split('-').map(Number);
    return new Date(parts[0], parts[1] - 1, parts[2]);
  }
  function addDays(date, n) { return new Date(date.getFullYear(), date.getMonth(), date.getDate() + n); }
  function dayKey(date) { return date.getFullYear() + '-' + (date.getMonth() + 1) + '-' + date.getDate(); }
  function short(date) { return date.getDate() + ' ' + EN_MONTHS[date.getMonth()].slice(0, 3); }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
  function cleanNumber(x) { return Number(x).toFixed(2); }

  // every request ends: it answers, fails, or is cut off after TIMEOUT_MS
  function timedFetch(url, options) {
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, TIMEOUT_MS) : null;
    var opts = {};
    Object.keys(options || {}).forEach(function (key) { opts[key] = options[key]; });
    if (controller) opts.signal = controller.signal;
    var done = function () { if (timer) clearTimeout(timer); };
    return fetch(url, opts).then(function (reply) { done(); return reply; }, function (error) { done(); throw error; });
  }

  // the status and the JSON body, with the body read inside the same time limit
  function timedJson(url, options) {
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, TIMEOUT_MS) : null;
    var opts = {};
    Object.keys(options || {}).forEach(function (key) { opts[key] = options[key]; });
    if (controller) opts.signal = controller.signal;
    var done = function () { if (timer) clearTimeout(timer); };
    return fetch(url, opts).then(function (reply) {
      return reply.json().catch(function () { return {}; }).then(function (body) {
        done();
        return { status: reply.status, ok: reply.ok, body: body || {} };
      });
    }).catch(function (error) { done(); throw error; });
  }

  function timedOut(error) { return !!(error && error.name === 'AbortError'); }

  // ---------- today's leaf ----------

  function setLeaf(prefix, date) {
    var en = EN_MONTHS[date.getMonth()].toUpperCase() + ' ' + date.getFullYear();
    var hi = HI_MONTHS[date.getMonth()] + ' ' + date.getFullYear();
    $(prefix + 'month-en').textContent = en;
    $(prefix + 'month-hi').textContent = hi;
    $(prefix + 'num').textContent = String(date.getDate());
    $(prefix + 'hi').textContent = HI_DAYS[date.getDay()];
    $(prefix + 'lat').textContent = EN_DAYS[date.getDay()].toUpperCase();
  }

  function paintToday() {
    var now = new Date();
    var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    setLeaf('today-', today);
    $('today-date').textContent = EN_DAYS[today.getDay()] + ', ' + today.getDate() + ' ' + EN_MONTHS[today.getMonth()] + ' ' + today.getFullYear();
    setLeaf('prev-', addDays(today, -1));
    var prev = document.querySelector('.prev');
    if (prev) prev.addEventListener('animationend', function () { prev.remove(); });
    return today;
  }

  // ---------- the file slots ----------

  var slots = {};
  document.querySelectorAll('.slot').forEach(function (slot) {
    var kind = slot.getAttribute('data-kind');
    var input = slot.querySelector('.file');
    var kindText = slot.querySelector('.kind');
    var pickEn = slot.querySelector('.pick-en');
    var clear = slot.querySelector('.clear');
    function refresh() {
      var file = input.files && input.files[0];
      slot.classList.toggle('filled', !!file);
      kindText.textContent = file ? file.name : kindText.getAttribute('data-default');
      pickEn.textContent = file ? 'Change' : 'Choose file';
      clear.hidden = !file;
      state.questions = [];
      renderQuestions();
      hideProblem();
    }
    input.addEventListener('change', refresh);
    clear.addEventListener('click', function () {
      input.value = '';
      refresh();
      input.focus();
    });
    slot.addEventListener('dragover', function (event) { event.preventDefault(); slot.classList.add('over'); });
    slot.addEventListener('dragleave', function () { slot.classList.remove('over'); });
    slot.addEventListener('drop', function (event) {
      event.preventDefault();
      slot.classList.remove('over');
      if (event.dataTransfer && event.dataTransfer.files.length) {
        try {
          var keep = new DataTransfer();
          keep.items.add(event.dataTransfer.files[0]);
          input.files = keep.files;
        } catch (e) { input.files = event.dataTransfer.files; }
        refresh();
      }
    });
    slots[kind] = { input: input, refresh: refresh };
  });

  function chosen(kind) { var f = slots[kind].input.files; return f && f[0]; }

  // ---------- column questions ----------

  function renderQuestions() {
    var list = $('ask-list');
    list.textContent = '';
    $('ask').hidden = !state.questions.length;
    state.questions.forEach(function (q, i) {
      var id = 'ask-' + i;
      var select = el('select', { id: id, 'data-file': q.file, 'data-name': q.name }, [
        el('option', { value: '', text: 'Pick a column' })
      ]);
      q.options.forEach(function (option) { select.appendChild(el('option', { value: option, text: option })); });
      select.addEventListener('change', function () { select.classList.remove('missing'); });
      list.appendChild(el('div', { class: 'ask-q' }, [el('label', { for: id, text: q.ask }), select]));
    });
    $('go-en').textContent = state.questions.length ? 'Check again with these columns' : 'Check the stock';
  }

  function pickedColumns() {
    var picked = { sales: {}, stock: {} };
    var missing = null;
    $('ask-list').querySelectorAll('select').forEach(function (select) {
      if (!select.value) {
        select.classList.add('missing');
        if (!missing) missing = select;
      } else {
        picked[select.getAttribute('data-file')][select.getAttribute('data-name')] = select.value;
      }
    });
    return { picked: picked, missing: missing };
  }

  // ---------- problems, busy state, progress ----------

  function recordedOk() { return !state.config || state.config.recorded_sample !== false; }

  // withSample adds the way out that always works: the recorded sample run
  function showProblem(text, next, withSample) {
    $('problem-text').textContent = text;
    $('problem-next').textContent = next || '';
    $('problem-next').hidden = !next;
    $('problem-action').hidden = !(withSample && recordedOk());
    $('problem').hidden = false;
  }
  function hideProblem() { $('problem').hidden = true; }

  function nextStepFor(status) {
    var mb = state.config ? state.config.limits.max_upload_mb : 5;
    switch (status) {
      case 413: return 'Pick a smaller file, at most ' + mb + ' MB, or run it on your own laptop for big files.';
      case 429: return 'Wait a little and try again. The free host only runs a few checks at a time.';
      case 503: return 'Try again later, or run it on your own laptop (see the end of the page).';
      case 404: return 'Run the check again. Finished checks are forgotten after 30 minutes.';
      case 422: return state.lastSample ? 'Try again in a moment.' : 'Check that each file went into the right slot, then try again. The sample shows the expected layout.';
      default: return 'Try again in a few minutes.';
    }
  }

  var ticks = $('ticks');
  for (var t = 0; t < TICKS; t++) ticks.appendChild(el('i'));

  // whole seconds, rounded to 5 so the number does not flicker on every poll
  function aboutSeconds(seconds) {
    var s = Math.max(5, Math.round(seconds / 5) * 5);
    return s + ' seconds';
  }

  // the counted line under the stage words, and the time a check of this size takes
  function setCount(stage, body) {
    var count = $('run-count'), items = body && body.items;
    var text = '';
    if (items && stage === 'predicting') text = 'Predicted ' + body.predicted + ' of ' + items + ' items.';
    else if (items && stage === 'testing') text = 'Tested ' + body.tested + ' of ' + items + ' items against last week.';
    else if (items && stage === 'wording') text = 'Predicted all ' + items + ' items.';
    if (text && stage !== 'wording' && body.eta_seconds != null) {
      text += body.eta_seconds >= 5 ? ' About ' + aboutSeconds(body.eta_seconds) + ' left.' : ' Almost done.';
    }
    if (count.textContent !== text) count.textContent = text;
    count.hidden = !text;
    var time = items && body.estimate_seconds != null
      ? ' ' + items + ' ' + (items === 1 ? 'item takes' : 'items take') + ' about ' + aboutSeconds(body.estimate_seconds) + '.'
      : '';
    if ($('run-time').textContent !== time) $('run-time').textContent = time;
  }

  function setProgress(stage, progress, body) {
    var info = STAGES[stage] || STAGES.reading;
    var pct = Math.max(0, Math.min(100, Math.round((progress || 0) * 100)));
    if ($('run-stage').textContent !== info[0]) $('run-stage').textContent = info[0];
    setCount(stage, body);
    $('run-step').textContent = String(info[1]);
    ticks.setAttribute('aria-valuenow', String(pct));
    ticks.setAttribute('aria-valuetext', info[0] + ', ' + pct + ' percent');
    var on = Math.round(pct / 100 * TICKS);
    // on narrow screens only the first 15 cells show, so they count the same share
    var shown = window.matchMedia('(max-width: 860px)').matches ? 15 : TICKS;
    var onShown = Math.round(pct / 100 * shown);
    Array.prototype.forEach.call(ticks.children, function (cell, i) { cell.classList.toggle('on', i < (shown === TICKS ? on : onShown)); });
  }

  // quiet keeps the running stages hidden, for a sample that may answer from the recorded run at once
  function setBusy(busy, label, quiet) {
    state.busy = busy;
    var go = $('go');
    go.disabled = busy || checksOff() || state.quotaOut;
    go.setAttribute('aria-busy', busy ? 'true' : 'false');
    $('sample').disabled = busy || (checksOff() && !(state.config && state.config.recorded_sample));
    $('run').hidden = !busy || !!quiet;
    $('go-en').textContent = busy ? (label || 'Checking the stock') : (state.questions.length ? 'Check again with these columns' : 'Check the stock');
  }

  function checksOff() { return !!(state.config && state.config.tabpfn === 'off'); }

  // ---------- starting and following a check ----------

  function startCheck(useSample) {
    if (state.busy) return;
    // the recorded sample needs no model, so it still works when checks are off or the quota is used up
    if (checksOff() && !(useSample && state.config.recorded_sample)) return;
    if (state.quotaOut && !useSample) return;
    hideProblem();
    var form = new FormData();
    state.lastSample = useSample;
    if (useSample) {
      form.append('use_sample', 'true');
    } else {
      var sales = chosen('sales'), stock = chosen('stock');
      if (!sales || !stock) {
        var missing = !(sales || stock) ? 'both reports' : sales ? 'the stock report too' : 'the sales report too';
        showProblem('Add ' + missing + ' to continue.', 'Or press "Try it with sample data" to see it work first.');
        return;
      }
      form.append('sales', sales, sales.name);
      form.append('stock', stock, stock.name);
      if (state.questions.length) {
        var cols = pickedColumns();
        if (cols.missing) {
          showProblem('Pick a column for each question above.', '');
          cols.missing.focus();
          return;
        }
        form.append('columns', JSON.stringify(cols.picked));
      }
    }
    form.append('language', state.language);
    setBusy(true, useSample ? 'Checking the sample' : 'Checking your stock', useSample);
    setProgress('reading', 0);

    timedJson('/api/checks', { method: 'POST', body: form }).then(function (reply) {
      var body = reply.body;
      if (reply.status === 202 && body.job_id) {
        state.jobId = body.job_id;
        if (!useSample) { state.questions = []; renderQuestions(); }
        if (body.recorded) { poll(body.job_id, 0, Date.now(), 0); return; } // already done, read it at once
        $('run').hidden = false;
        poll(body.job_id, 0, Date.now());
        return;
      }
      setBusy(false);
      if (reply.status === 422 && body.needs_columns && body.needs_columns.length) {
        state.questions = body.needs_columns;
        renderQuestions();
        var first = $('ask-list').querySelector('select');
        if (first) first.focus();
        return;
      }
      if (body.quota) {
        showQuotaUsedUp();
        showProblem(body.problem, '', true);
        return;
      }
      showProblem(body.problem || ('The server answered with an error (' + reply.status + ').'), nextStepFor(reply.status));
    }).catch(function (error) {
      setBusy(false);
      if (timedOut(error)) {
        showProblem('The server did not answer in time.', 'Try again in a few minutes.', !useSample);
      } else {
        showProblem('Could not reach the server.', 'It may still be waking up, which can take about a minute. Wait a moment and press the button again.', !useSample);
      }
    });
  }

  function poll(jobId, failures, started, wait) {
    setTimeout(function () {
      if (jobId !== state.jobId) return;
      if (Date.now() - started > POLL_GIVE_UP_MS) {
        state.jobId = null;
        setBusy(false);
        showProblem('The check is taking far longer than it should, so I stopped waiting for it.', 'Try again in a few minutes.', true);
        return;
      }
      timedJson('/api/checks/' + encodeURIComponent(jobId)).then(function (reply) {
        var body = reply.body;
        if (jobId !== state.jobId) return;
        if (reply.status === 404) {
          setBusy(false);
          showProblem(body.problem || 'This check is no longer on the server.', nextStepFor(404));
          return;
        }
        if (!reply.ok) throw new Error('status ' + reply.status);
        if (body.state === 'done') {
          setProgress('wording', 1);
          setBusy(false);
          showResult(body.result);
          return;
        }
        if (body.state === 'failed') {
          setBusy(false);
          if (body.quota) {
            showQuotaUsedUp();
            showProblem(body.problem, '', true);
          } else {
            showProblem(body.problem || 'The check stopped halfway.', 'Nothing was kept. Try again in a few minutes.', true);
          }
          return;
        }
        $('run').hidden = false;
        setProgress(body.stage, body.progress, body);
        poll(jobId, 0, started);
      }).catch(function () {
        if (jobId !== state.jobId) return;
        if (failures < 4) { poll(jobId, failures + 1, started); return; }
        state.jobId = null;
        setBusy(false);
        showProblem('Lost touch with the server while the check was running.', 'Check your connection, then run the check again.', true);
      });
    }, wait === undefined ? POLL_MS : wait);
  }

  $('check').addEventListener('submit', function (event) { event.preventDefault(); startCheck(false); });
  $('sample').addEventListener('click', function () { startCheck(true); });
  $('problem-action').addEventListener('click', function () {
    state.jobId = null;
    startCheck(true);
  });

  // ---------- the week ----------

  var today = paintToday();

  function dayLeaf(date, index) {
    var dow = date.getDay();
    var li = el('li', { class: 'day' + (dow === 0 ? ' sun' : '') });
    var showMonth = index === 0 || date.getDate() === 1;
    li.appendChild(el('div', { class: 'dn' }, [
      el('span', { class: 'd', text: String(date.getDate()) }),
      el('span', { class: 'wk' }, [
        el('b', { lang: 'hi', text: HI_DAYS[dow] }),
        el('span', { text: EN_DAYS[dow].toUpperCase() }),
        showMonth ? el('span', { class: 'mo', text: EN_MONTHS[date.getMonth()].slice(0, 3).toUpperCase() }) : null
      ])
    ]));
    return li;
  }

  function orderWords(line) {
    var q = el('span', { class: 'q' });
    if (line.order_packs && line.order_packs !== line.order_units) {
      q.appendChild(document.createTextNode('order '));
      q.appendChild(el('em', { text: plural(line.order_packs, 'pack', 'packs') }));
      q.appendChild(document.createTextNode(' (' + plural(line.order_units, 'piece', 'pieces') + ')'));
    } else {
      q.appendChild(document.createTextNode('order '));
      q.appendChild(el('em', { text: plural(line.order_units, 'piece', 'pieces') }));
    }
    return q;
  }

  function noteLine(index) {
    var text = state.lines[index] || '';
    // the marks follow the language shown: the note reply carries its own per-line flags
    var byAi = state.linesByAi[index];
    var marks = byAi !== undefined;
    var p = el('p', { class: 'line' + (marks ? '' : ' nomark') });
    if (marks) {
      var mark = byAi ? penIcon() : plainIcon();
      p.appendChild(mark);
      p.appendChild(el('span', { class: 'sr', text: byAi ? 'Worded by Gemma: ' : 'Plain sentence: ' }));
    }
    p.appendChild(el('span', { lang: LANG_ATTR[state.linesLanguage] || 'en', text: text }));
    return p;
  }

  function itemNode(index) {
    var line = state.result.plan[index];
    return el('li', { class: 'item' }, [
      el('strong', { text: line.item }),
      orderWords(line),
      noteLine(index)
    ]);
  }

  function renderEmptyWeek() {
    var days = $('days');
    days.textContent = '';
    for (var i = 1; i <= 7; i++) days.appendChild(dayLeaf(addDays(today, i), i - 1));
  }

  function renderWeek(deal) {
    var result = state.result;
    var start = addDays(parseDay(result.last_day), 1);
    var week = [];
    for (var i = 0; i < 7; i++) week.push(addDays(start, i));
    var end = week[6];
    $('week-stamp').textContent = 'Week of ' + short(start) + ' to ' + short(end) + ', the 7 days after ' +
      (state.resultSample ? "the sample's last sale" : 'your last sale');
    $('sample-stamp').hidden = !state.resultSample;
    var recordedNote = $('recorded-note');
    recordedNote.hidden = !result.recorded;
    if (result.recorded && result.recorded_on) {
      var on = parseDay(result.recorded_on);
      recordedNote.textContent = 'Recorded run from ' + short(on) + ' ' + on.getFullYear() +
        '. Upload the two sample files to run it live.';
    }

    var byDay = {};
    var gone = [];
    result.plan.forEach(function (line, index) {
      if (line.already_out) { gone.push(index); return; }
      var when = parseDay(line.runs_out_on);
      if (when < start) when = start;
      if (when > end) when = end;
      var key = dayKey(when);
      (byDay[key] = byDay[key] || []).push(index);
    });
    // the earliest run-out day among items still on the shelf
    var earliest = null;
    week.forEach(function (date) { if (!earliest && byDay[dayKey(date)]) earliest = dayKey(date); });
    state.firstIndex = earliest ? byDay[earliest][0] : (gone.length ? gone[0] : null);

    var goneBox = $('gone'), goneList = $('gone-list');
    goneList.textContent = '';
    goneBox.hidden = !gone.length;
    gone.forEach(function (index) { goneList.appendChild(itemNode(index)); });

    var days = $('days');
    days.textContent = '';
    week.forEach(function (date, i) {
      var li = dayLeaf(date, i);
      var items = byDay[dayKey(date)];
      if (items) {
        var ul = el('ul');
        items.forEach(function (index) { ul.appendChild(itemNode(index)); });
        li.appendChild(ul);
        if (dayKey(date) === earliest) {
          li.classList.add('urgent');
          li.appendChild(el('span', { class: 'first' }, [
            el('span', { lang: 'hi', text: 'सबसे पहले खत्म' }),
            el('span', { text: 'Runs out first' })
          ]));
        }
      } else {
        li.appendChild(el('p', { class: 'none' }, [
          el('span', { lang: 'hi', text: 'कुछ खत्म नहीं होगा' }),
          el('span', { text: 'Nothing runs out' })
        ]));
      }
      days.appendChild(li);
    });

    renderSay();
    if (deal && !reduced && days.animate) {
      Array.prototype.forEach.call(days.children, function (leaf, i) {
        leaf.animate([
          { transform: 'translateY(-34px) rotate(-1.2deg)', clipPath: 'inset(0 0 100% 0)' },
          { transform: 'translateY(0) rotate(0deg)', clipPath: 'inset(0 0 0% 0)' }
        ], { duration: 640, delay: 140 + i * 120, easing: 'cubic-bezier(0.16, 1, 0.3, 1)', fill: 'backwards' });
      });
    }
  }

  function renderSay() {
    var say = $('say');
    say.className = 'say';
    say.textContent = '';
    var total = state.result.plan.length;
    if (!total) {
      $('gemma-limited').hidden = true;
      say.appendChild(el('p', { class: 'say-big', text: 'Nothing to order this week.' }));
      say.appendChild(el('p', { class: 'say-small', text: 'Every checked item has enough stock for the next 7 days.' }));
      return;
    }
    // the first run-out item's sentence, in the language shown
    var first = state.firstIndex === null || state.firstIndex === undefined ? 0 : state.firstIndex;
    say.appendChild(el('p', { class: 'say-big', lang: LANG_ATTR[state.linesLanguage] || 'en', text: state.lines[first] || '' }));
    var small = el('p', { class: 'say-small' }, ['Gemma worded ' + state.wordedByAi + ' of ' + total + ' lines. The rest are the plain sentence.']);
    if (state.linesByAi.length === total) {
      small.appendChild(el('span', { class: 'legends' }, [
        el('span', { class: 'legend' }, [penIcon(), ' worded by Gemma']),
        el('span', { class: 'legend' }, [plainIcon(), ' plain sentence'])
      ]));
    }
    say.appendChild(small);
    $('gemma-limited').hidden = !state.gemmaLimited;
  }

  // ---------- small print under the week ----------

  function fine(label, count, items, prose) {
    var details = el('details', { class: 'fine' });
    var summary = el('summary', null, [plusIcon(), el('span', null, [el('b', { text: String(count) }), ' ' + label])]);
    details.appendChild(summary);
    var ul = el('ul', { class: prose ? 'prose' : null });
    items.forEach(function (text) { ul.appendChild(el('li', { text: text })); });
    details.appendChild(el('div', { class: 'body' }, [ul]));
    return details;
  }

  function renderLists() {
    var r = state.result;
    var box = $('lists');
    box.textContent = '';
    if (r.capped) {
      var capText = r.capped.text || ('This online check looked at the ' + r.capped.kept + ' items closest to running out, ' +
        'picked by a plain average. ' + r.capped.dropped + ' other ' + (r.capped.dropped === 1 ? 'item was' : 'items were') + ' left out.');
      box.appendChild(el('p', { text: capText + ' The laptop version checks every item.' }));
    }
    if (r.notes.length) box.appendChild(fine('notes on what was left out of the files, and why', r.notes.length, r.notes, true));
    if (r.enough_stock.length) {
      var enough = fine(r.enough_stock.length === 1 ? 'item has enough stock for the next 7 days' : 'items have enough stock for the next 7 days', r.enough_stock.length, r.enough_stock);
      if (!r.plan.length) enough.open = true;
      box.appendChild(enough);
    }
    if (r.not_checked.length) box.appendChild(fine('sold items have no stock count, so they were not checked', r.not_checked.length, r.not_checked));
    if (r.rare_items.length) box.appendChild(fine('items sold on fewer than 3 days, too rare to predict', r.rare_items.length, r.rare_items));
    box.hidden = !box.children.length;
  }

  // ---------- order list ----------

  function renderOrder() {
    var r = state.result;
    $('order').hidden = false;
    var empty = $('order-empty'), full = $('order-full');
    if (!r.plan.length) {
      empty.textContent = '';
      empty.appendChild(el('p', { class: 'big', text: 'Nothing to order this week.' }));
      empty.appendChild(el('p', { text: 'Every checked item has enough stock for the next 7 days. The list of them is under the week above.' }));
      empty.hidden = false;
      full.hidden = true;
      return;
    }
    empty.hidden = true;
    full.hidden = false;
    var body = $('order-lines');
    body.textContent = '';
    r.plan.forEach(function (line, i) {
      var tickId = 'tick-' + i, qtyId = 'qty-' + i;
      var tick = el('input', { type: 'checkbox', class: 'tick', id: tickId, checked: true, 'aria-label': 'Order ' + line.item });
      var qty = el('input', { type: 'number', class: 'qty', id: qtyId, min: '0', step: '1', inputmode: 'numeric',
        value: String(line.order_units), 'aria-label': 'Quantity of ' + line.item + ', in pieces' });
      var row = el('tr', null, [
        el('td', { class: 'ncell', text: String(i + 1) }),
        el('td', null, [el('label', { class: 'tick-hit', for: tickId }, [tick])]),
        el('td', { class: 'it' }, [line.item, el('small', { text: line.already_out ? 'Already finished' :
          'Runs out ' + EN_DAYS[parseDay(line.runs_out_on).getDay()] + ' ' + short(parseDay(line.runs_out_on)) +
          (line.order_packs && line.order_packs !== line.order_units ? ', ' + plural(line.order_packs, 'pack', 'packs') : '') })]),
        el('td', { class: 'qcell' }, [qty])
      ]);
      tick.addEventListener('change', function () { row.classList.toggle('off', !tick.checked); refreshOrderButtons(); });
      qty.addEventListener('input', refreshOrderButtons);
      body.appendChild(row);
    });
    $('order-date').textContent = today.getDate() + ' ' + EN_MONTHS[today.getMonth()] + ' ' + today.getFullYear();
    $('shop-name').value = r.shop_name || '';
    $('out-status').textContent = '';
    $('fallback').hidden = true;
    resetCopy();
    refreshOrderButtons();
  }

  function orderBody() {
    var lines = [];
    state.result.plan.forEach(function (line, i) {
      var tick = $('tick-' + i), qty = $('qty-' + i);
      var n = Math.max(0, Math.round(Number(qty.value) || 0));
      if (tick.checked && n > 0) lines.push({ item: line.item, quantity: n });
    });
    return { shop_name: $('shop-name').value.trim(), address: $('shop-address').value.trim(), lines: lines };
  }

  function refreshOrderButtons() {
    if (!state.result || !state.result.plan.length) return;
    var none = !orderBody().lines.length;
    $('copy').disabled = none;
    $('print').disabled = none;
    $('receipt').disabled = none || state.printing;
    $('any-printer').disabled = none || state.printing;
    if (state.printing) return;
    var status = $('out-status');
    status.classList.toggle('bad', none);
    status.textContent = none ? 'Tick at least one item to make an order list.' : '';
  }

  function resetCopy() {
    var copy = $('copy');
    copy.classList.remove('done');
    copy.querySelector('.bl-hi').textContent = 'WhatsApp के लिए कॉपी करें';
    copy.querySelector('.bl-en').textContent = 'Copy for WhatsApp';
  }

  function post(url, body) {
    return timedFetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  }

  function outProblem(text) {
    var status = $('out-status');
    status.classList.add('bad');
    status.textContent = text;
  }

  $('copy').addEventListener('click', function () {
    var copy = $('copy');
    var body = orderBody();
    if (!body.lines.length) return;
    copy.setAttribute('aria-busy', 'true');
    $('out-status').classList.remove('bad');
    $('out-status').textContent = 'Making the order list';
    post('/api/orders/text', body).then(function (reply) {
      return reply.json().then(function (data) {
        if (!reply.ok) throw new Error(data.problem || 'The order list could not be made.');
        return data.text;
      });
    }).then(function (text) {
      var done = function () {
        copy.classList.add('done');
        copy.querySelector('.bl-hi').textContent = 'कॉपी हो गया';
        copy.querySelector('.bl-en').textContent = 'Copied';
        $('out-status').textContent = 'Copied. Open WhatsApp and paste it to the supplier.';
        $('fallback').hidden = true;
        setTimeout(resetCopy, 4000);
      };
      var fallback = function () {
        $('fallback-text').value = text;
        $('fallback').hidden = false;
        $('out-status').textContent = '';
        $('fallback-text').focus();
        $('fallback-text').select();
      };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback);
      else fallback();
    }).catch(function (error) {
      outProblem(error && error.message && error.message.indexOf('fetch') === -1 ? error.message : 'Could not reach the server. Try again.');
    }).then(function () { copy.removeAttribute('aria-busy'); });
  });

  $('print').addEventListener('click', function () {
    var button = $('print');
    var body = orderBody();
    if (!body.lines.length) return;
    // open the tab now, while the click still counts, then fill it
    var tab = window.open('', '_blank');
    button.setAttribute('aria-busy', 'true');
    $('out-status').classList.remove('bad');
    $('out-status').textContent = 'Making the order sheet';
    post('/api/orders/sheet', body).then(function (reply) {
      if (!reply.ok) return reply.json().catch(function () { return {}; }).then(function (data) { throw new Error(data.problem || 'The order sheet could not be made.'); });
      return reply.text();
    }).then(function (html) {
      var url = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
      if (tab && !tab.closed) {
        try { tab.opener = null; } catch (e) { /* ignore */ }
        tab.location.href = url;
        $('out-status').textContent = 'The order sheet opened in a new tab. Print it from there.';
      } else {
        var status = $('out-status');
        status.textContent = 'Your browser blocked the new tab. ';
        status.appendChild(el('a', { href: url, target: '_blank', rel: 'noopener', text: 'Open the order sheet' }));
      }
      setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
    }).catch(function (error) {
      if (tab && !tab.closed) tab.close();
      outProblem(error && error.message && error.message.indexOf('fetch') === -1 ? error.message : 'Could not reach the server. Try again.');
    }).then(function () { button.removeAttribute('aria-busy'); });
  });

  // ---------- receipt printer, over Web Bluetooth ----------

  // serial-style write channels of cheap receipt printers, tried in this order. the first is the one
  // the laptop version uses (receipt_printer.WRITE_CHANNEL)
  var RECEIPT_CHANNELS = [
    ['49535343-fe7d-4ae5-8fa9-9fafd205e455', '49535343-8841-43f4-a8d4-ecbe34729bb3'],
    ['000018f0-0000-1000-8000-00805f9b34fb', '00002af1-0000-1000-8000-00805f9b34fb'],
    ['e7810a71-73ae-499d-8c15-faa9aef0c3f2', 'bef8d6c9-9c21-4c9e-b632-bd58c1009f9f']
  ];
  var RECEIPT_SERVICES = RECEIPT_CHANNELS.map(function (pair) { return pair[0]; });
  // receipt_printer.PRINTER_NAME_HINTS plus names these printers often advertise
  var PRINTER_NAMES = ['PSF', 'SR588', 'MPT', 'POS', 'PRINTER', 'RPP', 'PT-', 'Printer', 'BlueTooth Printer', 'MTP', 'EVOFOX'];
  var CONNECT_ATTEMPTS = 3;
  var CHUNK_BYTES = 100, SMALL_CHUNK_BYTES = 20;
  var RECEIPT_TEXT = {
    reach: 'Could not reach the printer. Switch it on, keep it near, and try again.',
    channel: 'This printer does not offer a channel I know how to print on.',
    network: 'Could not reach the server. Try again.'
  };

  function canPrintReceipt() {
    return !!(navigator.bluetooth && window.isSecureContext && navigator.bluetooth.requestDevice);
  }

  function wait(ms) { return new Promise(function (done) { setTimeout(done, ms); }); }

  function failure(kind, message) {
    var error = new Error(message || RECEIPT_TEXT[kind]);
    error.kind = kind;
    return error;
  }

  function receiptStatus(text, bad) {
    var status = $('out-status');
    status.classList.toggle('bad', !!bad);
    status.textContent = text;
  }

  function setPrinting(on) {
    state.printing = on;
    var button = $('receipt');
    if (on) button.setAttribute('aria-busy', 'true');
    else button.removeAttribute('aria-busy');
    $('paper').querySelectorAll('input').forEach(function (input) { input.disabled = on; });
    var none = !state.result || !orderBody().lines.length;
    button.disabled = on || none;
    $('any-printer').disabled = on || none;
  }

  function connectPrinter(device, attemptsLeft) {
    return device.gatt.connect().catch(function () {
      // the first knock often fails with no reason given, the next one works
      if (attemptsLeft <= 1) throw failure('reach');
      return wait(800).then(function () { return connectPrinter(device, attemptsLeft - 1); });
    });
  }

  function writable(characteristic) {
    return characteristic.properties && (characteristic.properties.write || characteristic.properties.writeWithoutResponse);
  }

  function anyWriteChannel(server) {
    return server.getPrimaryServices().then(function (services) {
      var i = 0;
      function nextService() {
        if (i >= services.length) throw failure('channel');
        var service = services[i++];
        return service.getCharacteristics().then(function (list) {
          var found = list.filter(writable)[0];
          return found || nextService();
        }, nextService);
      }
      return nextService();
    }, function () { throw failure('channel'); });
  }

  function writeChannel(server) {
    function known(i) {
      if (i >= RECEIPT_CHANNELS.length) return anyWriteChannel(server);
      return server.getPrimaryService(RECEIPT_CHANNELS[i][0])
        .then(function (service) { return service.getCharacteristic(RECEIPT_CHANNELS[i][1]); })
        .then(function (characteristic) { return writable(characteristic) ? characteristic : known(i + 1); },
          function () { return known(i + 1); });
    }
    return known(0);
  }

  function writeReceipt(device, characteristic, bytes) {
    var withResponse = !!characteristic.properties.write;
    var size = CHUNK_BYTES;
    function writeChunk(chunk) {
      if (withResponse) {
        return characteristic.writeValueWithResponse ? characteristic.writeValueWithResponse(chunk) : characteristic.writeValue(chunk);
      }
      var sent = characteristic.writeValueWithoutResponse ? characteristic.writeValueWithoutResponse(chunk) : characteristic.writeValue(chunk);
      return sent.then(function () { return wait(30); });
    }
    function from(start) {
      if (start >= bytes.length) return Promise.resolve();
      var chunk = bytes.slice(start, start + size);
      return writeChunk(chunk).then(function () { return from(start + chunk.length); }, function (error) {
        // a printer with a small packet size refuses 100 bytes at once, 20 always fits
        var tooBig = error && (error.name === 'InvalidModificationError' || error.name === 'NotSupportedError' ||
          /size|length|long|mtu/i.test(error.message || ''));
        if (size > SMALL_CHUNK_BYTES && (tooBig || start === 0) && device.gatt.connected) {
          size = SMALL_CHUNK_BYTES;
          return from(start);
        }
        throw failure('reach');
      });
    }
    return from(0);
  }

  function chosenPaper() {
    var checked = document.querySelector('#paper input:checked');
    return checked ? checked.value : '2 inch (58 mm)';
  }

  function printReceipt(anyDevice) {
    if (state.printing || !canPrintReceipt()) return;
    var body = orderBody();
    if (!body.lines.length) return;
    body.paper = chosenPaper();
    var options = anyDevice
      ? { acceptAllDevices: true, optionalServices: RECEIPT_SERVICES }
      : { filters: PRINTER_NAMES.map(function (name) { return { namePrefix: name }; })
          .concat(RECEIPT_SERVICES.map(function (service) { return { services: [service] }; })),
          optionalServices: RECEIPT_SERVICES };
    // the chooser must open before anything is awaited, or the click no longer counts
    var chosen;
    try { chosen = navigator.bluetooth.requestDevice(options); } catch (error) { chosen = Promise.reject(error); }
    setPrinting(true);
    receiptStatus('Choose your printer in the list that opened.');
    var device = null, bytes = null;
    chosen.then(function (picked) {
      device = picked;
      receiptStatus('Getting the receipt ready');
      return post('/api/orders/receipt', body).catch(function () { throw failure('network'); });
    }).then(function (reply) {
      if (!reply.ok) {
        return reply.json().catch(function () { return {}; }).then(function (data) {
          throw failure('server', data.problem || 'The receipt could not be made. Try again.');
        });
      }
      return reply.arrayBuffer();
    }).then(function (buffer) {
      bytes = new Uint8Array(buffer);
      receiptStatus('Connecting to ' + (device.name || 'the printer'));
      return connectPrinter(device, CONNECT_ATTEMPTS);
    }).then(function (server) {
      return writeChannel(server);
    }).then(function (characteristic) {
      receiptStatus('Printing');
      return writeReceipt(device, characteristic, bytes);
    }).then(function () {
      return wait(1000); // let the last lines print before the link closes
    }).then(function () {
      receiptStatus('Printed. Tear the paper off.');
    }).catch(function (error) {
      if (!device && error && error.name === 'NotFoundError') {
        receiptStatus(''); // the visitor closed the chooser
        return;
      }
      if (error && error.kind) receiptStatus(error.message, true);
      else if (!device) receiptStatus('The printer list could not open. Try again.', true);
      else receiptStatus(RECEIPT_TEXT.reach, true);
    }).then(function () {
      try { if (device && device.gatt && device.gatt.connected) device.gatt.disconnect(); } catch (e) { /* already gone */ }
      setPrinting(false);
    });
  }

  if (canPrintReceipt()) {
    $('receipt').hidden = false;
    $('paper').hidden = false;
    $('receipt-note').hidden = false;
    $('receipt').addEventListener('click', function () { printReceipt(false); });
    $('any-printer').addEventListener('click', function () { printReceipt(true); });
  } else {
    $('receipt-unsupported').hidden = false;
  }

  // ---------- honesty test ----------

  function renderHonesty() {
    var r = state.result;
    var section = $('honest'), box = $('honest-body');
    box.textContent = '';
    if (!r.honesty && !r.honesty_problem) { section.hidden = true; return; }
    section.hidden = false;
    if (!r.honesty) {
      box.appendChild(el('p', { class: 'honest-problem', text: r.honesty_problem }));
      return;
    }
    var h = r.honesty;
    var rows = [['Plain average', h.plain, false], ['Same-weekday average', h.same_weekday, false], ['TabPFN (the AI)', h.tabpfn, true]];
    var tbody = el('tbody');
    rows.forEach(function (row) {
      tbody.appendChild(el('tr', { class: row[2] ? 'win' : null }, [
        el('th', { scope: 'row', text: row[0] }),
        el('td', { class: 'n', text: cleanNumber(row[1].daily_error) }),
        el('td', { class: 'n', text: cleanNumber(row[1].weekly_error) })
      ]));
    });
    var table = el('table', { class: 'ledger' }, [
      el('caption', { text: 'This check, the hidden week. Off by, pieces per item, lower is better' }),
      el('thead', null, [el('tr', null, [
        el('th', { scope: 'col', text: 'Guess' }), el('th', { scope: 'col', class: 'n', text: 'Per day' }), el('th', { scope: 'col', class: 'n', text: 'Per week' })
      ])]),
      tbody
    ]);
    var covered = Math.round(h.busy_week_covered * 100);
    var notes = el('div', { class: 'honest-notes' }, [
      el('p', { class: 'cover', text: 'The busy-week level covered ' + covered + ' percent of the items in the hidden week.' }),
      el('p', { text: 'The two averages need no AI. They are the guesses TabPFN has to beat. It does not always win, least of all with only a few weeks of sales.' }),
      el('p', { text: 'A sudden bulk sale, like one customer buying a lot of wire, is something no guess can see coming.' })
    ]);
    if (r.capped) notes.appendChild(el('p', { text: 'This test covers only the items picked above, so it is not a score for the whole shop.' }));
    box.appendChild(el('div', { class: 'honest-grid' }, [table, notes]));

    var per = el('table', { class: 'ledger' }, [
      el('caption', { class: 'sr', text: 'Each item in the hidden week' }),
      el('thead', null, [el('tr', null, [
        el('th', { scope: 'col', text: 'Item' }),
        el('th', { scope: 'col', class: 'n', text: 'Really sold' }),
        el('th', { scope: 'col', class: 'n', text: 'TabPFN' }),
        el('th', { scope: 'col', class: 'n', text: 'Plain average' }),
        el('th', { scope: 'col', class: 'n', text: 'Same weekday' })
      ])])
    ]);
    var pb = el('tbody');
    h.per_item.forEach(function (row) {
      pb.appendChild(el('tr', null, [
        el('th', { scope: 'row', text: row.item }),
        el('td', { class: 'n', text: String(row.really_sold) }),
        el('td', { class: 'n', text: String(row.tabpfn) }),
        el('td', { class: 'n', text: String(row.plain) }),
        el('td', { class: 'n', text: String(row.same_weekday) })
      ]));
    });
    per.appendChild(pb);
    var details = el('details', { class: 'fine peritem' }, [
      el('summary', null, [plusIcon(), el('span', null, [el('b', { text: String(h.per_item.length) }), ' items, what really sold that week against each guess'])]),
      el('div', { class: 'body scroll' }, [per])
    ]);
    box.appendChild(details);
  }

  // ---------- the whole result ----------

  function showResult(result) {
    state.result = result;
    state.resultSample = state.lastSample;
    state.lines = result.plan.map(function (line) { return line.line; });
    state.linesLanguage = result.language;
    state.linesByAi = result.plan.map(function (line) { return line.by_ai; });
    state.wordedByAi = result.worded_by_ai;
    state.gemmaLimited = !!result.gemma_limited;
    setLanguageRadio(result.language);

    var summary = (result.shop_name ? result.shop_name + ': r' : 'R') + 'ead sales of ' + plural(result.items_sold, 'item', 'items') +
      ' over ' + plural(result.days_of_sales, 'day', 'days') + ' (' + short(parseDay(result.first_day)) + ' to ' + short(parseDay(result.last_day)) +
      '), and stock counts for ' + plural(result.stock_items, 'item', 'items') + '. Checked ' + plural(result.items_checked, 'item', 'items') + '.';
    $('summary').textContent = summary;
    $('summary').hidden = false;

    renderWeek(true);
    renderLists();
    renderOrder();
    renderHonesty();
    $('week').scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
  }

  // ---------- note language ----------

  function setLanguageRadio(language) {
    document.querySelectorAll('#lang input').forEach(function (input) { input.checked = input.value === language; });
    state.language = language;
  }

  document.querySelectorAll('#lang input').forEach(function (input) {
    input.addEventListener('change', function () {
      var language = input.value;
      if (!state.result) { state.language = language; return; }
      if (language === state.linesLanguage) { state.language = language; return; }
      var fieldset = $('lang');
      var previous = state.linesLanguage;
      fieldset.setAttribute('aria-busy', 'true');
      fieldset.querySelectorAll('input').forEach(function (r) { r.disabled = true; });
      var say = $('say');
      say.className = 'say';
      say.textContent = 'Wording the note in ' + language + '. This takes a few seconds.';
      post('/api/checks/' + encodeURIComponent(state.jobId) + '/note', { language: language }).then(function (reply) {
        return reply.json().catch(function () { return {}; }).then(function (data) {
          if (!reply.ok) throw new Error(data.problem || 'The note could not be worded in ' + language + '.');
          return data;
        });
      }).then(function (data) {
        state.lines = data.lines;
        state.linesLanguage = language;
        state.linesByAi = (data.plan || []).map(function (line) { return line.by_ai; });
        state.language = language;
        state.wordedByAi = data.worded_by_ai;
        state.gemmaLimited = !!data.gemma_limited;
        renderWeek(false);
      }).catch(function (error) {
        setLanguageRadio(previous);
        renderSay();
        say.className = 'say problem-line';
        var msg = error && error.message && error.message.indexOf('fetch') === -1 ? error.message : 'Could not reach the server.';
        say.textContent = msg + ' The note stays in ' + previous + '.';
      }).then(function () {
        fieldset.removeAttribute('aria-busy');
        fieldset.querySelectorAll('input').forEach(function (r) { r.disabled = false; });
      });
    });
  });

  // ---------- server settings ----------

  function applyConfig(config) {
    state.config = config;
    var items = config.limits.max_items;
    document.querySelectorAll('[data-limit="items"]').forEach(function (node) {
      node.textContent = items ? items + ' items' : 'every item';
    });
    document.querySelectorAll('[data-limit="mb"]').forEach(function (node) {
      node.textContent = config.limits.max_upload_mb + ' MB';
    });
    $('gemma-off').hidden = config.gemma !== 'off';
    if (config.tabpfn === 'off') {
      var go = $('go');
      go.classList.add('off');
      setBusy(false);
      showProblem('Checks are switched off right now: the forecasting model is not set up on this server.',
        'The measured results below still stand, and the laptop version runs fully on its own.', true);
    }
    renderQuota(config.quota);
  }

  // ---------- the free quota ----------

  function renderQuota(quota) {
    var line = $('quota-line');
    var left = quota ? quota.live_checks_left_today : null;
    state.quotaOut = left === 0;
    line.classList.toggle('used-up', state.quotaOut);
    if (left === null || left === undefined) {
      line.hidden = true;
    } else if (left === 0) {
      line.textContent = recordedOk()
        ? "Today's free quota for live checks is used up. The recorded sample run still works."
        : "Today's free quota for live checks is used up.";
      line.hidden = false;
    } else {
      line.textContent = 'About ' + plural(left, 'live check', 'live checks') + ' left today on the free quota.';
      line.hidden = false;
    }
    $('go').classList.toggle('off', state.quotaOut || checksOff());
    $('sample').classList.toggle('lead', state.quotaOut && recordedOk());
    if (!state.busy) setBusy(false);
  }

  function showQuotaUsedUp() {
    var quota = state.config && state.config.quota ? state.config.quota : { resets: null };
    renderQuota({ live_checks_left_today: 0, resets: quota.resets });
  }

  timedFetch('/api/config').then(function (reply) { return reply.ok ? reply.json() : null; })
    .then(function (config) { if (config) applyConfig(config); })
    .catch(function () { /* the check itself reports a dead server */ });

  renderEmptyWeek();
})();
