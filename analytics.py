"""Twilio Analytics Dashboard — DC Academy / Stefania AI Setter"""

import os
import json
import logging
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request
from twilio.rest import Client as TwilioClient

logger = logging.getLogger(__name__)

analytics_bp = Blueprint("analytics", __name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")


def _twilio_client():
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# ── API Endpoints ──────────────────────────────────────────────────────────

@analytics_bp.route("/api/analytics/summary", methods=["GET"])
def api_summary():
    date_from = request.args.get("from", (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"))
    date_to = request.args.get("to", datetime.now().strftime("%Y-%m-%d"))
    try:
        client = _twilio_client()
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        delta_days = (end - start).days
        prev_start = start - timedelta(days=delta_days)
        prev_end = start

        # Current period
        calls = client.calls.list(start_time_after=start, start_time_before=end, limit=1000)
        messages = client.messages.list(date_sent_after=start, date_sent_before=end, limit=1000)

        # Previous period
        prev_calls = client.calls.list(start_time_after=prev_start, start_time_before=prev_end, limit=1000)
        prev_messages = client.messages.list(date_sent_after=prev_start, date_sent_before=prev_end, limit=1000)

        summary = _build_summary(calls, messages)
        prev_summary = _build_summary(prev_calls, prev_messages)

        # Compute deltas
        deltas = {}
        for key in ["total_calls", "total_messages", "avg_duration", "response_rate", "total_cost"]:
            curr = summary.get(key, 0)
            prev = prev_summary.get(key, 0)
            if prev > 0:
                deltas[key] = round(((curr - prev) / prev) * 100, 1)
            elif curr > 0:
                deltas[key] = 100.0
            else:
                deltas[key] = 0.0

        return jsonify({"summary": summary, "deltas": deltas, "period": {"from": date_from, "to": date_to}})
    except Exception:
        logger.exception("Analytics summary error")
        return jsonify({"error": "Failed to fetch data"}), 500


@analytics_bp.route("/api/analytics/calls", methods=["GET"])
def api_calls():
    date_from = request.args.get("from", (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"))
    date_to = request.args.get("to", datetime.now().strftime("%Y-%m-%d"))
    direction_filter = request.args.get("direction", "")
    status_filter = request.args.get("status", "")
    try:
        client = _twilio_client()
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        calls = client.calls.list(start_time_after=start, start_time_before=end, limit=500)

        result = []
        daily = {}
        hourly = [0] * 24
        top_numbers = {}

        for c in calls:
            if direction_filter and c.direction != direction_filter:
                continue
            if status_filter and c.status != status_filter:
                continue

            dt = c.start_time or c.date_created
            day = dt.strftime("%Y-%m-%d")
            hour = dt.hour

            daily[day] = daily.get(day, 0) + 1
            hourly[hour] += 1

            number = c.to if c.direction == "outbound-api" else c.from_
            top_numbers[number] = top_numbers.get(number, 0) + 1

            result.append({
                "sid": c.sid,
                "from": c.from_,
                "to": c.to,
                "direction": c.direction,
                "status": c.status,
                "duration": int(c.duration or 0),
                "price": float(c.price or 0),
                "timestamp": dt.isoformat(),
            })

        top_sorted = sorted(top_numbers.items(), key=lambda x: x[1], reverse=True)[:10]

        return jsonify({
            "calls": result,
            "daily": daily,
            "hourly": hourly,
            "top_numbers": [{"number": n, "count": c} for n, c in top_sorted],
        })
    except Exception:
        logger.exception("Analytics calls error")
        return jsonify({"error": "Failed to fetch calls"}), 500


@analytics_bp.route("/api/analytics/messages", methods=["GET"])
def api_messages():
    date_from = request.args.get("from", (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"))
    date_to = request.args.get("to", datetime.now().strftime("%Y-%m-%d"))
    direction_filter = request.args.get("direction", "")
    try:
        client = _twilio_client()
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        messages = client.messages.list(date_sent_after=start, date_sent_before=end, limit=500)

        result = []
        daily = {}

        for m in messages:
            if direction_filter:
                msg_dir = "inbound" if m.direction == "inbound" else "outbound"
                if msg_dir != direction_filter:
                    continue

            dt = m.date_sent or m.date_created
            day = dt.strftime("%Y-%m-%d")
            daily[day] = daily.get(day, 0) + 1

            msg_type = "whatsapp" if (m.from_ and "whatsapp" in m.from_) or (m.to and "whatsapp" in m.to) else "sms"

            result.append({
                "sid": m.sid,
                "from": m.from_,
                "to": m.to,
                "direction": m.direction,
                "status": m.status,
                "type": msg_type,
                "price": float(m.price or 0),
                "timestamp": dt.isoformat(),
            })

        return jsonify({"messages": result, "daily": daily})
    except Exception:
        logger.exception("Analytics messages error")
        return jsonify({"error": "Failed to fetch messages"}), 500


def _build_summary(calls, messages):
    total_calls = len(calls)
    answered = sum(1 for c in calls if c.status == "completed")
    durations = [int(c.duration or 0) for c in calls if c.status == "completed"]
    avg_duration = round(sum(durations) / len(durations), 1) if durations else 0
    response_rate = round((answered / total_calls) * 100, 1) if total_calls else 0

    call_cost = sum(abs(float(c.price or 0)) for c in calls)
    msg_cost = sum(abs(float(m.price or 0)) for m in messages)

    outbound_calls = sum(1 for c in calls if "outbound" in (c.direction or ""))
    inbound_calls = total_calls - outbound_calls

    whatsapp_msgs = sum(1 for m in messages if (m.from_ and "whatsapp" in m.from_) or (m.to and "whatsapp" in m.to))
    sms_msgs = len(messages) - whatsapp_msgs

    outbound_msgs = sum(1 for m in messages if m.direction != "inbound")
    inbound_msgs = len(messages) - outbound_msgs

    return {
        "total_calls": total_calls,
        "outbound_calls": outbound_calls,
        "inbound_calls": inbound_calls,
        "answered_calls": answered,
        "avg_duration": avg_duration,
        "response_rate": response_rate,
        "total_messages": len(messages),
        "whatsapp_msgs": whatsapp_msgs,
        "sms_msgs": sms_msgs,
        "outbound_msgs": outbound_msgs,
        "inbound_msgs": inbound_msgs,
        "call_cost": round(call_cost, 4),
        "msg_cost": round(msg_cost, 4),
        "total_cost": round(call_cost + msg_cost, 4),
    }


# ── Dashboard HTML ─────────────────────────────────────────────────────────

@analytics_bp.route("/analytics", methods=["GET"])
def analytics_page():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Twilio Analytics - DC Academy</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f23; color: #e0e0e0; padding: 16px; }
h1 { color: #fff; font-size: 24px; margin-bottom: 4px; }
.subtitle { color: #888; font-size: 14px; margin-bottom: 20px; }

/* Filters */
.filters { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; align-items: center; }
.filters input, .filters select { background: #1a1a2e; color: #e0e0e0; border: 1px solid #2a2a4a; padding: 8px 12px; border-radius: 6px; font-size: 14px; }
.filters button { background: #2196F3; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; }
.filters button:hover { background: #1976D2; }
.quick-btn { background: #1a1a2e !important; border: 1px solid #2a2a4a !important; }
.quick-btn:hover { background: #16213e !important; }
.quick-btn.active { background: #2196F3 !important; border-color: #2196F3 !important; }

/* Stats grid */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 24px; }
.stat { background: #1a1a2e; padding: 16px; border-radius: 10px; }
.stat-number { font-size: 28px; font-weight: bold; }
.stat-label { color: #888; font-size: 12px; margin-top: 4px; }
.stat-delta { font-size: 12px; margin-top: 4px; }
.delta-up { color: #4CAF50; }
.delta-down { color: #f44336; }
.delta-neutral { color: #888; }
.green { color: #4CAF50; }
.blue { color: #2196F3; }
.orange { color: #FF9800; }
.red { color: #f44336; }
.purple { color: #9C27B0; }
.cyan { color: #00BCD4; }

/* Sections */
.section { background: #1a1a2e; border-radius: 10px; margin-bottom: 12px; overflow: hidden; }
.section-header { padding: 14px 16px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; user-select: none; }
.section-header:hover { background: #16213e; }
.section-header h3 { font-size: 15px; color: #fff; }
.section-arrow { color: #888; transition: transform 0.2s; font-size: 12px; }
.section-arrow.open { transform: rotate(180deg); }
.section-body { display: none; padding: 0 16px 16px; }
.section-body.open { display: block; }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: #888; font-size: 11px; text-transform: uppercase; padding: 8px 6px; border-bottom: 1px solid #2a2a4a; }
td { padding: 8px 6px; border-bottom: 1px solid #1a1a2e; }
tr:hover { background: #16213e; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.badge-green { background: #1b5e20; color: #81c784; }
.badge-red { background: #b71c1c33; color: #ef9a9a; }
.badge-blue { background: #0d47a133; color: #90caf9; }
.badge-orange { background: #e65100; color: #ffcc80; }
.badge-purple { background: #4a148c33; color: #ce93d8; }

/* Charts */
.chart-container { max-height: 300px; margin-top: 10px; }
.charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 768px) { .charts-row { grid-template-columns: 1fr; } }

/* Loading */
.loading { text-align: center; padding: 40px; color: #888; }
.spinner { display: inline-block; width: 24px; height: 24px; border: 3px solid #2a2a4a; border-top-color: #2196F3; border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Cost detail */
.cost-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.cost-item { background: #16213e; padding: 12px; border-radius: 8px; text-align: center; }
.cost-item .cost-val { font-size: 20px; font-weight: bold; }
.cost-item .cost-lbl { font-size: 11px; color: #888; margin-top: 4px; }
</style>
</head>
<body>

<h1>Twilio Analytics</h1>
<p class="subtitle">DC Academy - Stefania AI Setter</p>

<div class="filters">
    <input type="date" id="date-from">
    <input type="date" id="date-to">
    <button onclick="loadAll()">Aggiorna</button>
    <button class="quick-btn" onclick="setRange(7)">7gg</button>
    <button class="quick-btn" onclick="setRange(14)">14gg</button>
    <button class="quick-btn" onclick="setRange(30)">30gg</button>
</div>

<div id="summary-area"><div class="loading"><div class="spinner"></div><p style="margin-top:10px">Caricamento dati...</p></div></div>

<!-- Breakdown giornaliero -->
<div class="section">
    <div class="section-header" onclick="toggleSection(this)">
        <h3>Breakdown Giornaliero</h3><span class="section-arrow">&#9660;</span>
    </div>
    <div class="section-body">
        <div class="charts-row">
            <div class="chart-container"><canvas id="chart-daily-calls"></canvas></div>
            <div class="chart-container"><canvas id="chart-daily-msgs"></canvas></div>
        </div>
    </div>
</div>

<!-- Distribuzione oraria -->
<div class="section">
    <div class="section-header" onclick="toggleSection(this)">
        <h3>Distribuzione Oraria Chiamate</h3><span class="section-arrow">&#9660;</span>
    </div>
    <div class="section-body">
        <div class="chart-container"><canvas id="chart-hourly"></canvas></div>
    </div>
</div>

<!-- Top numeri -->
<div class="section">
    <div class="section-header" onclick="toggleSection(this)">
        <h3>Top Numeri</h3><span class="section-arrow">&#9660;</span>
    </div>
    <div class="section-body" id="top-numbers-body"></div>
</div>

<!-- Dettaglio costi -->
<div class="section">
    <div class="section-header" onclick="toggleSection(this)">
        <h3>Dettaglio Costi</h3><span class="section-arrow">&#9660;</span>
    </div>
    <div class="section-body" id="cost-detail-body"></div>
</div>

<!-- Lista chiamate -->
<div class="section">
    <div class="section-header" onclick="toggleSection(this)">
        <h3>Lista Chiamate</h3><span class="section-arrow">&#9660;</span>
    </div>
    <div class="section-body" id="calls-table-body"></div>
</div>

<!-- Lista messaggi -->
<div class="section">
    <div class="section-header" onclick="toggleSection(this)">
        <h3>Lista Messaggi</h3><span class="section-arrow">&#9660;</span>
    </div>
    <div class="section-body" id="msgs-table-body"></div>
</div>

<script>
const charts = {};

function fmt(n) { return typeof n === 'number' ? n.toFixed(2) : n; }
function fmtDur(s) { return Math.floor(s/60) + ':' + String(Math.floor(s%60)).padStart(2,'0'); }
function fmtTime(iso) { return new Date(iso).toLocaleString('it-IT', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}); }

function deltaHtml(val, invert) {
    if (val === 0) return '<span class="delta-neutral">--</span>';
    const cls = (invert ? val < 0 : val > 0) ? 'delta-up' : 'delta-down';
    const arrow = val > 0 ? '&#9650;' : '&#9660;';
    return '<span class="' + cls + '">' + arrow + ' ' + Math.abs(val) + '%</span>';
}

function statusBadge(s) {
    const map = {completed:'badge-green',busy:'badge-orange','no-answer':'badge-red',failed:'badge-red',canceled:'badge-red',
                 delivered:'badge-green',sent:'badge-blue',read:'badge-green',undelivered:'badge-red',queued:'badge-orange'};
    return '<span class="badge ' + (map[s]||'badge-blue') + '">' + s + '</span>';
}

function dirBadge(d) {
    const out = d && d.includes('outbound');
    return '<span class="badge ' + (out?'badge-purple':'badge-blue') + '">' + (out?'OUT':'IN') + '</span>';
}

function typeBadge(t) {
    return '<span class="badge ' + (t==='whatsapp'?'badge-green':'badge-blue') + '">' + t.toUpperCase() + '</span>';
}

function setRange(days) {
    const to = new Date(); const from = new Date(); from.setDate(from.getDate() - days);
    document.getElementById('date-from').value = from.toISOString().split('T')[0];
    document.getElementById('date-to').value = to.toISOString().split('T')[0];
    document.querySelectorAll('.quick-btn').forEach(b => b.classList.remove('active'));
    event.target.classList.add('active');
    loadAll();
}

function getParams() {
    return 'from=' + document.getElementById('date-from').value + '&to=' + document.getElementById('date-to').value;
}

function toggleSection(el) {
    const body = el.nextElementSibling;
    const arrow = el.querySelector('.section-arrow');
    body.classList.toggle('open');
    arrow.classList.toggle('open');
}

async function loadAll() {
    const p = getParams();
    document.getElementById('summary-area').innerHTML = '<div class="loading"><div class="spinner"></div></div>';

    const [sumRes, callsRes, msgsRes] = await Promise.all([
        fetch('/api/analytics/summary?' + p).then(r => r.json()),
        fetch('/api/analytics/calls?' + p).then(r => r.json()),
        fetch('/api/analytics/messages?' + p).then(r => r.json()),
    ]);

    renderSummary(sumRes);
    renderCalls(callsRes);
    renderMessages(msgsRes);
}

function renderSummary(data) {
    const s = data.summary, d = data.deltas;
    document.getElementById('summary-area').innerHTML = `
    <div class="stats">
        <div class="stat"><div class="stat-number blue">${s.total_calls}</div>
            <div class="stat-label">Chiamate (${s.outbound_calls} out / ${s.inbound_calls} in)</div>
            <div class="stat-delta">${deltaHtml(d.total_calls)}</div></div>
        <div class="stat"><div class="stat-number green">${s.answered_calls}</div>
            <div class="stat-label">Risposte</div></div>
        <div class="stat"><div class="stat-number orange">${s.response_rate}%</div>
            <div class="stat-label">Tasso risposta</div>
            <div class="stat-delta">${deltaHtml(d.response_rate)}</div></div>
        <div class="stat"><div class="stat-number cyan">${fmtDur(s.avg_duration)}</div>
            <div class="stat-label">Durata media</div>
            <div class="stat-delta">${deltaHtml(d.avg_duration)}</div></div>
        <div class="stat"><div class="stat-number purple">${s.total_messages}</div>
            <div class="stat-label">Messaggi (${s.whatsapp_msgs} WA / ${s.sms_msgs} SMS)</div>
            <div class="stat-delta">${deltaHtml(d.total_messages)}</div></div>
        <div class="stat"><div class="stat-number red">$${s.total_cost.toFixed(2)}</div>
            <div class="stat-label">Costo totale</div>
            <div class="stat-delta">${deltaHtml(d.total_cost, true)}</div></div>
    </div>`;

    // Cost detail
    document.getElementById('cost-detail-body').innerHTML = `
    <div class="cost-grid">
        <div class="cost-item"><div class="cost-val blue">$${s.call_cost.toFixed(2)}</div><div class="cost-lbl">Chiamate</div></div>
        <div class="cost-item"><div class="cost-val green">$${s.msg_cost.toFixed(2)}</div><div class="cost-lbl">Messaggi</div></div>
        <div class="cost-item"><div class="cost-val orange">${s.total_calls > 0 ? '$' + (s.call_cost / s.total_calls).toFixed(3) : '--'}</div><div class="cost-lbl">Costo/Chiamata</div></div>
        <div class="cost-item"><div class="cost-val purple">${s.total_messages > 0 ? '$' + (s.msg_cost / s.total_messages).toFixed(3) : '--'}</div><div class="cost-lbl">Costo/Messaggio</div></div>
    </div>`;
}

function renderCalls(data) {
    // Daily chart
    const days = Object.keys(data.daily).sort();
    renderChart('chart-daily-calls', 'bar', days, Object.values(data.daily).length ? days.map(d => data.daily[d]) : [], 'Chiamate/giorno', '#2196F3');

    // Hourly chart
    const hours = Array.from({length:24}, (_,i) => i + ':00');
    renderChart('chart-hourly', 'bar', hours, data.hourly, 'Chiamate/ora', '#FF9800');

    // Top numbers
    let topHtml = '<table><tr><th>Numero</th><th>Chiamate</th></tr>';
    data.top_numbers.forEach(n => { topHtml += '<tr><td>' + n.number + '</td><td>' + n.count + '</td></tr>'; });
    topHtml += '</table>';
    document.getElementById('top-numbers-body').innerHTML = topHtml;

    // Calls table
    let html = '<table><tr><th>Data</th><th>Da</th><th>A</th><th>Dir</th><th>Stato</th><th>Durata</th><th>Costo</th></tr>';
    data.calls.sort((a,b) => b.timestamp.localeCompare(a.timestamp));
    data.calls.forEach(c => {
        html += '<tr><td>' + fmtTime(c.timestamp) + '</td><td>' + c.from + '</td><td>' + c.to + '</td><td>' + dirBadge(c.direction) + '</td><td>' + statusBadge(c.status) + '</td><td>' + fmtDur(c.duration) + '</td><td>$' + Math.abs(c.price).toFixed(3) + '</td></tr>';
    });
    html += '</table>';
    document.getElementById('calls-table-body').innerHTML = html;
}

function renderMessages(data) {
    const days = Object.keys(data.daily).sort();
    renderChart('chart-daily-msgs', 'bar', days, days.map(d => data.daily[d]), 'Messaggi/giorno', '#9C27B0');

    let html = '<table><tr><th>Data</th><th>Da</th><th>A</th><th>Tipo</th><th>Dir</th><th>Stato</th><th>Costo</th></tr>';
    data.messages.sort((a,b) => b.timestamp.localeCompare(a.timestamp));
    data.messages.forEach(m => {
        html += '<tr><td>' + fmtTime(m.timestamp) + '</td><td>' + m.from + '</td><td>' + m.to + '</td><td>' + typeBadge(m.type) + '</td><td>' + dirBadge(m.direction) + '</td><td>' + statusBadge(m.status) + '</td><td>$' + Math.abs(m.price).toFixed(3) + '</td></tr>';
    });
    html += '</table>';
    document.getElementById('msgs-table-body').innerHTML = html;
}

function renderChart(id, type, labels, data, label, color) {
    const ctx = document.getElementById(id);
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(ctx, {
        type: type,
        data: { labels: labels, datasets: [{ label: label, data: data, backgroundColor: color + '88', borderColor: color, borderWidth: 1 }] },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: { y: { beginAtZero: true, ticks: { color: '#888' }, grid: { color: '#1a1a2e' } },
                      x: { ticks: { color: '#888', maxRotation: 45 }, grid: { display: false } } }
        }
    });
}

// Init
(function() {
    const to = new Date(); const from = new Date(); from.setDate(from.getDate() - 7);
    document.getElementById('date-from').value = from.toISOString().split('T')[0];
    document.getElementById('date-to').value = to.toISOString().split('T')[0];
    loadAll();
})();
</script>
</body>
</html>"""
