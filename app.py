"""
Répondeur de Yassine — envoi automatique du compte rendu de chaque appel.

Quand un appel avec l'agent ElevenLabs se termine, ElevenLabs appelle ce serveur
(webhook post-appel « transcription »). Le serveur :
  1. vérifie la signature du webhook,
  2. récupère le résumé, la conversation complète et les infos collectées,
  3. télécharge l'enregistrement audio (MP3) de l'appel via l'API ElevenLabs,
  4. envoie le tout par email à Yassine (audio en pièce jointe).

Variables d'environnement à définir :
  ELEVENLABS_API_KEY   clé API ElevenLabs (droit de lecture sur ElevenAgents)
  WEBHOOK_SECRET       secret du webhook affiché par ElevenLabs (optionnel mais conseillé)
  GMAIL_USER           adresse Gmail qui envoie l'email
  GMAIL_APP_PASSWORD   mot de passe d'application Gmail (pas le mot de passe normal)
  DESTINATAIRE         adresse qui reçoit le compte rendu (obligatoire)
  EXPEDITEUR           adresse affichée en expéditeur, ex. un alias (optionnel)
"""

import hashlib
import hmac
import html
import os
import smtplib
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage

import requests
from flask import Flask, abort, request

API = "https://api.elevenlabs.io/v1"
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
DESTINATAIRE = os.environ.get("DESTINATAIRE", "")
# Adresse affichée comme expéditeur (un alias du compte). Par défaut : GMAIL_USER.
EXPEDITEUR = os.environ.get("EXPEDITEUR", "") or GMAIL_USER

app = Flask(__name__)
deja_traites: set[str] = set()  # évite les doublons si ElevenLabs renvoie le webhook


# ---------------------------------------------------------------- sécurité

def signature_valide(corps: bytes, entete: str) -> bool:
    """Vérifie l'entête ElevenLabs-Signature (format « t=<horodatage>,v0=<hmac> »)."""
    if not WEBHOOK_SECRET:
        return True  # pas de secret configuré : on n'effectue pas la vérification
    try:
        parties = dict(p.split("=", 1) for p in entete.split(","))
        horodatage, recu = parties["t"], parties["v0"]
    except (ValueError, KeyError):
        return False
    if abs(time.time() - int(horodatage)) > 30 * 60:  # webhook trop ancien
        return False
    attendu = hmac.new(
        WEBHOOK_SECRET.encode(), f"{horodatage}.{corps.decode()}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(attendu, recu)


# ---------------------------------------------------------------- données

def telecharger_audio(conversation_id: str) -> bytes | None:
    """Récupère le MP3 de l'appel (quelques essais, le temps qu'il soit prêt)."""
    for _ in range(5):
        r = requests.get(
            f"{API}/convai/conversations/{conversation_id}/audio",
            headers={"xi-api-key": ELEVENLABS_API_KEY},
            timeout=120,
        )
        if r.status_code == 200 and r.content:
            return r.content
        time.sleep(10)
    print(f"[{conversation_id}] audio indisponible ({r.status_code})")
    return None


def infos_collectees(analyse: dict) -> list[tuple[str, str]]:
    """Champs de « Collecte de données » (nom_appelant, numero_rappel, message...)."""
    lignes = []
    for cle, valeur in (analyse.get("data_collection_results") or {}).items():
        if isinstance(valeur, dict):
            valeur = valeur.get("value")
        if valeur not in (None, ""):
            lignes.append((cle.replace("_", " ").capitalize(), str(valeur)))
    return lignes


# ---------------------------------------------------------------- email

def construire_email(data: dict, audio: bytes | None) -> EmailMessage:
    meta = data.get("metadata") or {}
    analyse = data.get("analysis") or {}
    resume = analyse.get("transcript_summary") or "Aucun résumé disponible."
    infos = infos_collectees(analyse)

    debut = meta.get("start_time_unix_secs")
    quand = (
        datetime.fromtimestamp(debut, tz=timezone.utc).astimezone().strftime("%d/%m/%Y à %H:%M")
        if debut else "date inconnue"
    )
    duree = int(meta.get("call_duration_secs") or 0)
    duree_txt = f"{duree // 60} min {duree % 60:02d} s"

    # Conversation, ligne par ligne
    tours = [
        t for t in (data.get("transcript") or []) if (t.get("message") or "").strip()
    ]
    texte_conv = "\n".join(
        f"{'Agent' if t.get('role') == 'agent' else 'Appelant'} : {t['message'].strip()}"
        for t in tours
    ) or "(conversation vide)"
    html_conv = "".join(
        f"<p style='margin:6px 0'><b>{'Agent' if t.get('role') == 'agent' else 'Appelant'} :</b> "
        f"{html.escape(t['message'].strip())}</p>"
        for t in tours
    ) or "<p>(conversation vide)</p>"

    texte_infos = "\n".join(f"- {k} : {v}" for k, v in infos)
    html_infos = (
        "<h3>Informations laissées</h3><ul>"
        + "".join(f"<li><b>{html.escape(k)} :</b> {html.escape(v)}</li>" for k, v in infos)
        + "</ul>"
    ) if infos else ""

    msg = EmailMessage()
    msg["Subject"] = f"Nouvel appel sur ton répondeur — {quand}"
    msg["From"] = f"Répondeur Yassine <{EXPEDITEUR}>"
    msg["To"] = DESTINATAIRE

    msg.set_content(
        f"Nouvel appel le {quand} (durée : {duree_txt}).\n\n"
        f"RÉSUMÉ\n{resume}\n\n"
        + (f"INFORMATIONS LAISSÉES\n{texte_infos}\n\n" if infos else "")
        + f"CONVERSATION\n{texte_conv}\n\n"
        + ("L'enregistrement de l'appel est en pièce jointe." if audio
           else "L'enregistrement audio n'était pas disponible.")
    )
    msg.add_alternative(
        f"""<div style="font-family:Arial,sans-serif;max-width:640px;color:#222">
        <h2>Nouvel appel sur ton répondeur</h2>
        <p>{quand} · durée {duree_txt}</p>
        <h3>Résumé</h3><p>{html.escape(resume)}</p>
        {html_infos}
        <h3>Conversation</h3>
        <div style="background:#f5f5f5;padding:12px;border-radius:8px">{html_conv}</div>
        <p style="color:#666">{"🎧 L'enregistrement de l'appel est en pièce jointe." if audio
            else "L'enregistrement audio n'était pas disponible."}</p>
        </div>""",
        subtype="html",
    )

    if audio:
        msg.add_attachment(
            audio, maintype="audio", subtype="mpeg",
            filename=f"appel_{datetime.now().strftime('%Y%m%d_%H%M')}.mp3",
        )
    return msg


def envoyer(msg: EmailMessage) -> None:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


def traiter_appel(data: dict) -> None:
    conversation_id = data.get("conversation_id", "?")
    try:
        audio = telecharger_audio(conversation_id) if data.get("has_audio", True) else None
        envoyer(construire_email(data, audio))
        print(f"[{conversation_id}] email envoyé à {DESTINATAIRE}")
    except Exception as e:  # on log l'erreur sans faire planter le serveur
        print(f"[{conversation_id}] échec de l'envoi : {e}")


# ---------------------------------------------------------------- routes

@app.post("/webhook")
def webhook():
    corps = request.get_data()
    if not signature_valide(corps, request.headers.get("ElevenLabs-Signature", "")):
        abort(401)

    evenement = request.get_json(silent=True) or {}
    if evenement.get("type") != "post_call_transcription":
        return {"ignore": True}, 200

    data = evenement.get("data") or {}
    conversation_id = data.get("conversation_id")
    if conversation_id in deja_traites:
        return {"doublon": True}, 200
    deja_traites.add(conversation_id)

    # On répond tout de suite à ElevenLabs, l'email part en arrière-plan.
    threading.Thread(target=traiter_appel, args=(data,), daemon=True).start()
    return {"recu": True}, 200


@app.get("/")
def sante():
    return "Répondeur de Yassine : serveur actif."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
