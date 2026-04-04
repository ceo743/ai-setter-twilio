"""Stefania AI Setter — Analytics Dashboard with call analysis."""

import os
import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request
import httpx

logger = logging.getLogger(__name__)

analytics_bp = Blueprint("analytics", __name__)

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_CALLS_KEY = "call_history"


def _redis_request(command_args):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    try:
        resp = httpx.post(
            UPSTASH_REDIS_REST_URL,
            headers={"Authorization": "Bearer " + UPSTASH_REDIS_REST_TOKEN},
            json=command_args,
            timeout=10,
        )
        return resp.json().get("result")
    except Exception:
        logger.exception("Analytics Redis request failed")
        return None


def _load_calls(limit=500):
    result = _redis_request(["LRANGE", REDIS_CALLS_KEY, "0", str(limit - 1)])
    if result and isinstance(result, list):
        calls = []
        for item in result:
            try:
                calls.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                pass
        return calls
    return []


def _analyze_transcript(transcript):
    """Extract insights from a single transcript."""
    if not transcript:
        return {}
    lines = transcript.strip().split("\n")
    stefania_lines = [l for l in lines if l.startswith("Stefania:")]
    lead_lines = [l for l in lines if l.startswith("Lead:")]

    # Detect issues
    issues = []
    full_lower = transcript.lower()

    # Barge-in: Stefania repeats greeting
    greetings = [l for l in stefania_lines if "ciao" in l.lower() and "sono stefania" in l.lower()]
    if len(greetings) > 1:
        issues.append("barge-in-saluto")

    # Double question in one turn
    for line in stefania_lines:
        text = line.replace("Stefania:", "").strip()
        questions = [s.strip() for s in re.split(r'\?', text) if s.strip()]
        if len(questions) >= 2:
            issues.append("doppia-domanda")
            break

    # Said "perfetto" after nonsense
    if "perfetto" in full_lower and any(w in full_lower for w in ["luna", "marte", "giove", "tori", "capre"]):
        issues.append("perfetto-a-nonsense")

    # Promised email
    for line in stefania_lines:
        if any(w in line.lower() for w in ["mail", "email", "e-mail"]):
            if "segnalo" in line.lower() or "mand" in line.lower():
                issues.append("promessa-email")
                break

    # Detect objections handled
    objections = []
    objection_map = {
        "non ho tempo": "tempo",
        "quanto costa": "prezzo",
        "non mi interessa": "disinteresse",
        "gia' speso": "budget-speso",
        "già speso": "budget-speso",
        "sto guidando": "guidando",
        "non ricordo": "non-ricorda",
        "magari piu' avanti": "rimandare",
        "magari più avanti": "rimandare",
    }
    for lead_line in lead_lines:
        text = lead_line.lower()
        for trigger, label in objection_map.items():
            if trigger in text:
                objections.append(label)

    return {
        "stefania_turns": len(stefania_lines),
        "lead_turns": len(lead_lines),
        "total_turns": len(lines),
        "issues": issues,
        "objections": objections,
    }


def _build_analysis(calls):
    """Build comprehensive analysis from all calls."""
    if not calls:
        return {"empty": True}

    total = len(calls)
    statuses = Counter(c.get("status", "da confermare") for c in calls)
    qualificati = statuses.get("qualificato", 0)
    non_target = statuses.get("non in target", 0)
    da_confermare = statuses.get("da confermare", 0)

    # Budget distribution
    budgets = Counter(c.get("budget", "N/A") for c in calls if c.get("budget"))
    # Fatturato distribution
    fatturati = Counter(c.get("fatturato", "N/A") for c in calls if c.get("fatturato"))
    # Acquisition channels
    acquisizioni = Counter(c.get("acquisizione", "N/A") for c in calls if c.get("acquisizione"))
    # Roles
    ruoli = Counter(c.get("ruolo", "N/A") for c in calls if c.get("ruolo"))

    # Transcript analysis
    all_issues = []
    all_objections = []
    call_details = []

    for c in calls:
        analysis = _analyze_transcript(c.get("transcript", ""))
        all_issues.extend(analysis.get("issues", []))
        all_objections.extend(analysis.get("objections", []))
        call_details.append({
            "nome": "{} {}".format(c.get("nome", ""), c.get("cognome", "")).strip(),
            "phone": c.get("phone", ""),
            "status": c.get("status", ""),
            "timestamp": c.get("timestamp", ""),
            "ruolo": c.get("ruolo", ""),
            "obiettivi": c.get("obiettivi", ""),
            "budget": c.get("budget", ""),
            "turns": analysis.get("total_turns", 0),
            "issues": analysis.get("issues", []),
            "objections": analysis.get("objections", []),
            "has_transcript": bool(c.get("transcript")),
            "problemi": c.get("problemi", []),
        })

    issue_counts = Counter(all_issues)
    objection_counts = Counter(all_objections)

    # Patterns
    patterns = []
    if qualificati > 0 and total > 0:
        rate = round(qualificati / total * 100, 1)
        if rate >= 70:
            patterns.append({"type": "positive", "text": "Tasso qualificazione alto: {}% ({}/{})".format(rate, qualificati, total)})
        elif rate <= 30:
            patterns.append({"type": "warning", "text": "Tasso qualificazione basso: {}% ({}/{}) — verificare se i lead in ingresso sono in target".format(rate, qualificati, total)})
        else:
            patterns.append({"type": "info", "text": "Tasso qualificazione: {}% ({}/{})".format(rate, qualificati, total)})

    if issue_counts.get("barge-in-saluto", 0) > 0:
        patterns.append({"type": "warning", "text": "Barge-in rilevato in {} chiamate — Stefania ripete il saluto".format(issue_counts["barge-in-saluto"])})
    if issue_counts.get("doppia-domanda", 0) > 0:
        patterns.append({"type": "warning", "text": "Doppia domanda in {} chiamate — dovrebbe fare una domanda alla volta".format(issue_counts["doppia-domanda"])})
    if issue_counts.get("perfetto-a-nonsense", 0) > 0:
        patterns.append({"type": "warning", "text": "Ha detto 'perfetto' a risposte senza senso in {} chiamate".format(issue_counts["perfetto-a-nonsense"])})
    if issue_counts.get("promessa-email", 0) > 0:
        patterns.append({"type": "info", "text": "Ha promesso invio email in {} chiamate — verificare follow-up".format(issue_counts["promessa-email"])})

    if non_target > 0:
        patterns.append({"type": "info", "text": "{} lead non in target su {} — {}% di chiamate 'sprecate'".format(non_target, total, round(non_target/total*100, 1))})

    # Top objections
    if objection_counts:
        top_obj = objection_counts.most_common(3)
        patterns.append({"type": "info", "text": "Obiezioni piu' frequenti: " + ", ".join("{} ({}x)".format(o, c) for o, c in top_obj)})

    # Calls with no transcript (hung up fast)
    no_transcript = sum(1 for c in calls if not c.get("transcript"))
    if no_transcript > 0:
        patterns.append({"type": "warning", "text": "{} chiamate senza trascrizione — lead hanno riagganciato subito".format(no_transcript)})

    return {
        "total": total,
        "qualificati": qualificati,
        "non_target": non_target,
        "da_confermare": da_confermare,
        "qualification_rate": round(qualificati / total * 100, 1) if total else 0,
        "budgets": dict(budgets.most_common(10)),
        "fatturati": dict(fatturati.most_common(10)),
        "acquisizioni": dict(acquisizioni.most_common(10)),
        "ruoli": dict(ruoli.most_common(10)),
        "issue_counts": dict(issue_counts),
        "objection_counts": dict(objection_counts),
        "patterns": patterns,
        "calls": call_details,
    }


# ── API ────────────────────────────────────────────────────────────────────

@analytics_bp.route("/api/analytics", methods=["GET"])
def api_analytics():
    calls = _load_calls()
    analysis = _build_analysis(calls)
    return jsonify(analysis)


@analytics_bp.route("/api/analytics/transcript/<int:idx>", methods=["GET"])
def api_transcript(idx):
    calls = _load_calls()
    if 0 <= idx < len(calls):
        return jsonify({"transcript": calls[idx].get("transcript", "")})
    return jsonify({"error": "Not found"}), 404


# ── Dashboard HTML ─────────────────────────────────────────────────────────

@analytics_bp.route("/analytics", methods=["GET"])
def analytics_page():
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stefania Analytics - DC Academy</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f23; color: #e0e0e0; padding: 16px; max-width: 1200px; margin: 0 auto; }
h1 { color: #fff; font-size: 22px; margin-bottom: 4px; }
.subtitle { color: #888; font-size: 13px; margin-bottom: 20px; }

/* Stats */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 20px; }
.stat { background: #1a1a2e; padding: 14px; border-radius: 10px; text-align: center; }
.stat-number { font-size: 32px; font-weight: bold; }
.stat-label { color: #888; font-size: 11px; margin-top: 4px; }
.green { color: #4CAF50; } .red { color: #f44336; } .blue { color: #2196F3; }
.orange { color: #FF9800; } .purple { color: #9C27B0; } .cyan { color: #00BCD4; }

/* Patterns / Insights */
.insights { margin-bottom: 20px; }
.insight { padding: 10px 14px; border-radius: 8px; margin-bottom: 6px; font-size: 13px; display: flex; align-items: center; gap: 8px; }
.insight-positive { background: #1b5e2033; border-left: 3px solid #4CAF50; }
.insight-warning { background: #e6510033; border-left: 3px solid #FF9800; }
.insight-info { background: #0d47a133; border-left: 3px solid #2196F3; }
.insight-icon { font-size: 16px; }

/* Sections */
.section { background: #1a1a2e; border-radius: 10px; margin-bottom: 10px; overflow: hidden; }
.section-header { padding: 12px 14px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; }
.section-header:hover { background: #16213e; }
.section-header h3 { font-size: 14px; color: #fff; }
.section-arrow { color: #888; transition: transform 0.2s; font-size: 11px; }
.section-arrow.open { transform: rotate(180deg); }
.section-body { display: none; padding: 0 14px 14px; }
.section-body.open { display: block; }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; color: #888; font-size: 10px; text-transform: uppercase; padding: 6px 4px; border-bottom: 1px solid #2a2a4a; }
td { padding: 6px 4px; border-bottom: 1px solid #16213e; vertical-align: top; }
tr:hover { background: #16213e; }
.badge { display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; }
.badge-green { background: #1b5e20; color: #81c784; }
.badge-red { background: #b71c1c33; color: #ef9a9a; }
.badge-blue { background: #0d47a133; color: #90caf9; }
.badge-orange { background: #e6510033; color: #ffcc80; }

/* Distribution bars */
.dist-row { display: flex; align-items: center; margin-bottom: 6px; font-size: 12px; }
.dist-label { width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dist-bar-bg { flex: 1; background: #16213e; height: 18px; border-radius: 3px; margin: 0 8px; overflow: hidden; }
.dist-bar { height: 100%; border-radius: 3px; min-width: 2px; }
.dist-count { width: 30px; text-align: right; color: #888; }

/* Transcript modal */
.modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: #000a; z-index: 100; padding: 20px; overflow-y: auto; }
.modal.open { display: block; }
.modal-content { background: #1a1a2e; border-radius: 10px; padding: 20px; max-width: 700px; margin: 40px auto; max-height: 80vh; overflow-y: auto; }
.modal-close { float: right; background: none; border: none; color: #888; font-size: 20px; cursor: pointer; }
.transcript-text { white-space: pre-wrap; font-size: 13px; line-height: 1.6; }
.transcript-text .t-stefania { color: #81c784; }
.transcript-text .t-lead { color: #90caf9; }

/* Loading */
.loading { text-align: center; padding: 40px; color: #888; }
.spinner { display: inline-block; width: 24px; height: 24px; border: 3px solid #2a2a4a; border-top-color: #2196F3; border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Issue tags */
.issue-tag { display: inline-block; padding: 1px 5px; border-radius: 3px; font-size: 9px; margin: 1px; background: #e6510033; color: #ffcc80; }

button.refresh { background: #2196F3; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; margin-bottom: 16px; }
button.refresh:hover { background: #1976D2; }

@media (max-width: 600px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
    .dist-label { width: 120px; }
}
</style>
</head>
<body>

<h1>Stefania Analytics</h1>
<p class="subtitle">DC Academy - Analisi Chiamate AI Setter</p>
<button class="refresh" onclick="loadData()">Aggiorna dati</button>

<div id="content"><div class="loading"><div class="spinner"></div><p style="margin-top:10px">Caricamento...</p></div></div>
<div class="modal" id="modal"><div class="modal-content"><button class="modal-close" onclick="closeModal()">&times;</button><div id="modal-body"></div></div></div>

<script>
let DATA = null;

function statusBadge(s) {
    const map = {qualificato:'badge-green','non in target':'badge-red','da confermare':'badge-orange'};
    const labels = {qualificato:'Confermato','non in target':'Non Confermato','da confermare':'Da Confermare'};
    return '<span class="badge '+(map[s]||'badge-blue')+'">'+(labels[s]||s)+'</span>';
}

function toggleSection(el) {
    el.nextElementSibling.classList.toggle('open');
    el.querySelector('.section-arrow').classList.toggle('open');
}

function distBars(data, color, maxVal) {
    if (!maxVal) maxVal = Math.max(...Object.values(data), 1);
    return Object.entries(data).map(([k,v]) =>
        '<div class="dist-row"><span class="dist-label">'+k+'</span><div class="dist-bar-bg"><div class="dist-bar" style="width:'+Math.round(v/maxVal*100)+'%;background:'+color+'"></div></div><span class="dist-count">'+v+'</span></div>'
    ).join('');
}

function showTranscript(idx) {
    fetch('/api/analytics/transcript/'+idx).then(r=>r.json()).then(d => {
        let t = d.transcript || 'Nessuna trascrizione';
        t = t.replace(/^(Stefania:.*)/gm, '<span class="t-stefania">$1</span>')
             .replace(/^(Lead:.*)/gm, '<span class="t-lead">$1</span>');
        document.getElementById('modal-body').innerHTML = '<div class="transcript-text">'+t+'</div>';
        document.getElementById('modal').classList.add('open');
    });
}
function closeModal() { document.getElementById('modal').classList.remove('open'); }
document.getElementById('modal').addEventListener('click', function(e) { if (e.target === this) closeModal(); });

async function loadData() {
    document.getElementById('content').innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    const res = await fetch('/api/analytics');
    DATA = await res.json();
    render(DATA);
}

function render(d) {
    if (d.empty) {
        document.getElementById('content').innerHTML = '<div class="loading"><p>Nessuna chiamata registrata. I dati appariranno dopo le prossime chiamate.</p></div>';
        return;
    }

    let html = '';

    // Stats
    html += '<div class="stats">';
    html += '<div class="stat"><div class="stat-number blue">'+d.total+'</div><div class="stat-label">Chiamate totali</div></div>';
    html += '<div class="stat"><div class="stat-number green">'+d.qualificati+'</div><div class="stat-label">Confermati</div></div>';
    html += '<div class="stat"><div class="stat-number red">'+d.non_target+'</div><div class="stat-label">Non in target</div></div>';
    html += '<div class="stat"><div class="stat-number orange">'+d.da_confermare+'</div><div class="stat-label">Da confermare</div></div>';
    html += '<div class="stat"><div class="stat-number cyan">'+d.qualification_rate+'%</div><div class="stat-label">Tasso qualificazione</div></div>';
    html += '</div>';

    // Insights
    if (d.patterns && d.patterns.length) {
        html += '<div class="insights">';
        const icons = {positive:'&#10004;', warning:'&#9888;', info:'&#9432;'};
        d.patterns.forEach(p => {
            html += '<div class="insight insight-'+p.type+'"><span class="insight-icon">'+(icons[p.type]||'')+'</span> '+p.text+'</div>';
        });
        html += '</div>';
    }

    // Obiezioni
    if (d.objection_counts && Object.keys(d.objection_counts).length) {
        html += '<div class="section"><div class="section-header" onclick="toggleSection(this)"><h3>Obiezioni Rilevate</h3><span class="section-arrow">&#9660;</span></div>';
        html += '<div class="section-body">'+distBars(d.objection_counts, '#FF9800')+'</div></div>';
    }

    // Problemi tecnici
    if (d.issue_counts && Object.keys(d.issue_counts).length) {
        html += '<div class="section"><div class="section-header" onclick="toggleSection(this)"><h3>Problemi Tecnici Rilevati</h3><span class="section-arrow">&#9660;</span></div>';
        html += '<div class="section-body">'+distBars(d.issue_counts, '#f44336')+'</div></div>';
    }

    // Lead profile distributions
    if (d.budgets && Object.keys(d.budgets).length) {
        html += '<div class="section"><div class="section-header" onclick="toggleSection(this)"><h3>Distribuzione Budget</h3><span class="section-arrow">&#9660;</span></div>';
        html += '<div class="section-body">'+distBars(d.budgets, '#9C27B0')+'</div></div>';
    }
    if (d.ruoli && Object.keys(d.ruoli).length) {
        html += '<div class="section"><div class="section-header" onclick="toggleSection(this)"><h3>Ruoli Lead</h3><span class="section-arrow">&#9660;</span></div>';
        html += '<div class="section-body">'+distBars(d.ruoli, '#2196F3')+'</div></div>';
    }
    if (d.acquisizioni && Object.keys(d.acquisizioni).length) {
        html += '<div class="section"><div class="section-header" onclick="toggleSection(this)"><h3>Canali di Acquisizione</h3><span class="section-arrow">&#9660;</span></div>';
        html += '<div class="section-body">'+distBars(d.acquisizioni, '#00BCD4')+'</div></div>';
    }

    // Call list
    html += '<div class="section"><div class="section-header" onclick="toggleSection(this)"><h3>Tutte le Chiamate ('+d.calls.length+')</h3><span class="section-arrow">&#9660;</span></div>';
    html += '<div class="section-body"><table><tr><th>Data</th><th>Nome</th><th>Ruolo</th><th>Esito</th><th>Turni</th><th>Problemi</th><th></th></tr>';
    d.calls.forEach((c, i) => {
        let issues = (c.issues||[]).map(x => '<span class="issue-tag">'+x+'</span>').join(' ');
        html += '<tr><td>'+c.timestamp+'</td><td>'+c.nome+'</td><td>'+(c.ruolo||'--')+'</td><td>'+statusBadge(c.status)+'</td><td>'+c.turns+'</td><td>'+issues+'</td>';
        html += '<td>'+(c.has_transcript ? '<a href="#" onclick="showTranscript('+i+');return false" style="color:#2196F3">Leggi</a>' : '--')+'</td></tr>';
    });
    html += '</table></div></div>';

    document.getElementById('content').innerHTML = html;
}

loadData();
</script>
</body>
</html>"""
