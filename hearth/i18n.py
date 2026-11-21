"""Lightweight i18n (German + English) for Hearth's UI.

Design: small flat key→string dictionaries, no framework. Server templates can call
`t(key)` via a context helper; the client loads its dictionary from /api/i18n and applies
it to elements with data-i18n attributes plus a few dynamic JS strings. A user's language
preference is stored on User.language ("de" | "en") and returned by /auth/me.

To add a string: add the key to BOTH _EN and _DE below, then reference it in templates
(data-i18n="key" or t("key")) or JS (I18N.t("key")). Missing keys fall back to English,
then to the key itself — so the UI never renders blank.
"""
from __future__ import annotations

SUPPORTED = ("en", "de")
DEFAULT_LANG = "en"

_EN = {
    "app.name": "Hearth",
    "chat.new": "+ New chat",
    "chat.signout": "Sign out",
    "chat.admin": "Admin",
    "chat.placeholder": "Message Hearth… (try: list / read notes.md / write notes.txt Hello / run echo hi)",
    "chat.send": "Send",
    "chat.debug": "debug",
    "chat.model": "model",
    # Model badge states (passive /healthz probe — see DEC-048). The badge never
    # starts a model, so when nothing is up it points at where to start one.
    "chat.model_none": "not running",
    "chat.model_up_title": "a llama-server is answering on the configured port",
    "chat.model_down_title": "no llama-server is running — start it under Admin → Model",
    "chat.artifacts": "Artifacts",
    "chat.artifacts.empty": "No files yet. Files the assistant writes or you upload appear here.",
    "chat.art_this": "this chat",
    "chat.art_all": "all",
    "chat.search": "Search chats…",
    "chat.rename": "Rename",
    "chat.delete": "Delete",
    "chat.delete_confirm": "Delete this conversation? Choose OK to also delete its files, or Cancel-style option below.",
    "chat.delete_files_q": "Also delete the files (artifacts) from this conversation?",
    "chat.rename_prompt": "New name for this conversation:",
    "chat.upload": "Attach files",
    # The size is interpolated by chat.js from the server's uploads.max_bytes, so this
    # string must not bake in a number that Admin can change (AUDIT #8).
    "chat.upload_too_big": "File too large (over the upload limit)",
    "chat.upload_replaced": "a file with this name already existed and was replaced — its previous contents are gone",
    # Composer attach menu (replaces the standalone 📎/📷 buttons; add future
    # composer actions here instead of growing the input row).
    "chat.attach_menu": "Attach",
    "chat.am_camera": "Take photo",
    "chat.am_gallery": "Photo from gallery",
    "chat.am_proj_into": "Uploads \u2192 project",
    "chat.am_proj_hint": "Send uploads into the linked project folder instead of attaching them to this chat",
    # Linked project folder (DEC-049) — header chip + composer upload toggle.
    "chat.proj_into": "\u2192 project",
    "chat.proj_clean": "Linked project: no pending changes (click to refresh)",
    "chat.proj_pending": "Pending project changes \u2014 click to review and apply",
    "chat.proj_conflict": "Changes are blocked: files changed in the real folder since import. Re-import or discard your edits (Admin \u2192 Project).",
    "chat.proj_err": "Could not read the linked project",
    "chat.proj_review": "Project changes",
    "chat.proj_apply": "Apply to folder",
    "chat.proj_writing": "Writing\u2026",
    "chat.proj_blocked": "Blocked \u2014 these changed in the real folder since import:",
    "chat.proj_blocked_hint": "Re-import (Admin \u2192 Project) to adopt the folder's version, or undo your edits. Nothing can be applied while a conflict exists.",
    "chat.proj_disabled": "Apply is unavailable on this system (no write backend, or workspace encryption at rest is on).",
    "chat.proj_nothing": "Nothing to apply.",
    "chat.proj_confirm": "Write these changes to the real folder?",
    "chat.proj_confirm2": "Overwritten files are backed up first.",
    "chat.proj_backup": "backup",
    "chat.proj_failed": "Apply failed.",
    "chat.drop_here": "Drop files to upload",
    "chat.camera": "Camera / Photo",
    "chat.device_allow": "Allow device access",
    "chat.device_granted": "Device access: allowed",
    "chat.device_note": "Attaching files and downloading them never needs this switch — that is your own file transfer, not device access.",
    "chat.device_hint": "A device helper is paired. Device tools (folder listing, screenshots) need per-chat access — ask an admin for details.",
    "chat.abort": "Stop",
    "chat.stop": "Stop",
    "chat.aborting": "Stopping…",
    "chat.aborted": "Turn aborted.",
    "chat.retry": "↻ Retry",
    "chat.retrying": "retrying…",
    "chat.force_respond": "⚡ Force respond",
    "chat.force_armed": "⚡ next message answers with no tools",
    "chat.agent_mode": "🤖 Agent mode: on",
    "chat.agent_mode_on": "Agent mode: on",
    "chat.chat_only": "Chat only (no tools)",
    "chat.thinking": "thinking…",
    "chat.generating": "generating",
    "chat.writing": "writing",
    "chat.composing_cmd": "composing command",
    "chat.await_approval": "awaiting approval",
    "chat.still_running": "a turn is still running in this chat — Stop to end it",
    "chat.ctx_title": "Tokens the model received on the last turn / configured window",
    "chat.ctx_est": "estimated",
    "chat.ctx_warn": "Context is nearly full: start a new chat or shorten files.",
    "chat.download": "Download",
    # Files panel: the filename is the download link, everything else is in the ⋮ menu.
    "chat.art_open_dl": "Download (or use ⋮ for more options)",
    "chat.art_attach": "Attach to message",
    "chat.art_attached": "Attached to the next message.",
    "chat.art_already_attached": "Already attached.",
    "chat.art_preview": "Open preview",
    "chat.art_copy_link": "Copy link",
    "chat.art_link_copied": "Link copied.",
    "chat.art_copy_failed": "Could not copy the link.",
    "chat.art_delete_confirm": "Delete this file? It is removed from your workspace too.",
    "chat.art_delete_failed": "Could not delete the file.",
    "chat.intro_head": "How files work here:",
    "chat.intro_body": "Ask for a file and the assistant writes it into your own workspace — every file is listed under Artifacts (this chat / all), where you can download it. You get an explicit ⬇ download button in the conversation only when you ask for one. Writing shows live progress while it happens.",
    "chat.intro_dismiss": "Got it",
    "chat.approve": "Approve",
    "chat.deny": "Deny",
    "chat.reply": "Reply",
    "chat.tool": "tool",
    # display_media bubbles (chat.js → addMediaBubble). Full screen uses the browser's own
    # fullscreen API, so there is no custom viewer to translate beyond these labels.
    "chat.media_fullscreen": "Full screen",
    "chat.media_download": "Download",
    "chat.media_unsupported": "Your browser cannot play this file type.",
    "chat.media_missing": "This file is no longer in your workspace.",
    "chat.web_sources": "Sources",
    "chat.web_read": "read page",
    "chat.web_mode": "via",
    "chat.prefill": "prefill",
    # Audio captioner: shown while a second model listens to an attached recording. The wait
    # is real (ffmpeg + listening), so it must never look like a hang. See hearth/captioner.py.
    "chat.listening": "listening to the audio…",
    "chat.caption_failed": "I could not listen to that file — the note in my message says why.",
    # Header of the collapsible block showing the captioner model's words as they stream in.
    "chat.caption_streaming": "Audio model",
    # Shown on a restored caption whose description failed, so an old failure is not mistaken
    # for a missing feature after a reload (DEC-061).
    "chat.caption_failed_short": "could not describe",

    # Speed labels for the measured rates llama.cpp reports (see hearth/perf.py). The UI
    # shows these in preference to Hearth's own wall-clock tok/s estimate.
    "chat.decode": "decode",
    # Collapsible header for the model's thinking block (live and in history).
    "chat.reasoning": "Reasoning",
    # Steering queue row (message sent while a turn owns the transcript).
    "chat.steer_queued": "Queued for the next step",
    "chat.steer_withdraw": "Withdraw",
    "chat.steer_hint": "A turn is running — this will be read between tool steps.",
    # ETA suffix on the live prefill label ("prefill 43 % · ≈1m 20s left").
    "chat.left": "left",
    "chat.protocol": "Protocol",
    "chat.menu": "Menu",
    "chat.close_menu": "Close menu",
    "chat.more_options": "More options",
    "nav.chats": "Chats",
    "nav.files": "Files",
    "nav.prompt": "System prompt",
    "nav.skills": "Skills",
    "nav.chats_short": "Chats",
    "nav.files_short": "Files",
    "nav.prompt_short": "Prompt",
    "nav.skills_short": "Skills",
    "nav.settings_short": "Setup",
    "prompt.title": "System prompt",
    "prompt.hint": "The system prompt sent with every chat, from the next message on. Edit it directly.",
    "prompt.save": "Save",
    "prompt.saved": "Saved ✓",
    "prompt.save_failed": "Could not save",
    "prompt.reset": "Clear",
    "prompt.reset_title": "Remove your personal prompt and use the built-in persona only",
    "prompt.placeholder_base": "Add your instructions here…",
    "skills.title": "Skills",
    "skills.hint": "Reusable instruction formats an admin provides.",
    "skills.none": "Nothing here yet.",
    "settings.title": "Settings",
    "settings.enter_sends": "Enter sends the message",
    "settings.enter_sends_hint": "On: Enter = send, Shift+Enter = new line. Off: Enter = new line, use the Send button (Ctrl/Cmd+Enter also sends).",
    "settings.auto_scroll": "Auto-scroll to the newest message",
    "settings.auto_scroll_hint": "On: the transcript follows output as it streams. Off: it stays where you left it.",
    "settings.timestamps": "Show message timestamps",
    "settings.timestamps_hint": "Small time chips on each bubble (browser local time).",
    "settings.debug_hint": "Show the raw event log panel under the chat.",
    "login.title": "Sign in · Hearth",
    "login.heading": "Hearth",
    "login.subtitle": "Private family LLM. Sign in.",
    "login.username": "Username",
    "login.password": "Password",
    "login.totp": "2FA code",
    "login.button": "Sign in",
    "login.note": "Access is restricted to invited accounts. If you reached this page by accident, please close it.",
    "login.failed": "Login failed",
    "common.language": "Language",
}

_DE = {
    "app.name": "Herdfeuer",
    "chat.new": "+ Neue Unterhaltung",
    "chat.signout": "Abmelden",
    "chat.admin": "Verwaltung",
    "chat.placeholder": "Nachricht an Hearth… (z. B.: list / read notes.md / write notizen.txt Hallo / run echo hi)",
    "chat.send": "Senden",
    "chat.debug": "Debug",
    "chat.model": "Modell",
    # Model badge states (passiver /healthz-Probe — siehe DEC-048). Das Badge startet
    # nie ein Modell; wenn keines läuft, verweist es darauf, wo man eines startet.
    "chat.model_none": "läuft nicht",
    "chat.model_up_title": "ein llama-server antwortet auf den konfigurierten Port",
    "chat.model_down_title": "kein llama-server aktiv — starten unter Verwaltung → Modell",
    "chat.artifacts": "Dateien",
    "chat.artifacts.empty": "Noch keine Dateien. Vom Assistenten erstellte oder hochgeladene Dateien erscheinen hier.",
    "chat.art_this": "dieser Chat",
    "chat.art_all": "alle",
    "chat.search": "Chats durchsuchen…",
    "chat.rename": "Umbenennen",
    "chat.delete": "Löschen",
    "chat.delete_confirm": "Diese Unterhaltung löschen?",
    "chat.delete_files_q": "Auch die Dateien (Artefakte) aus dieser Unterhaltung löschen?",
    "chat.rename_prompt": "Neuer Name für diese Unterhaltung:",
    "chat.upload": "Dateien anhängen",
    # Die Größe wird von chat.js aus uploads.max_bytes des Servers eingesetzt — keine
    # Zahl einbacken, die der Admin ändern kann (AUDIT #8).
    "chat.upload_too_big": "Datei zu groß (über dem Upload-Limit)",
    "chat.upload_replaced": "eine Datei mit diesem Namen existierte bereits und wurde ersetzt — ihr bisheriger Inhalt ist weg",
    # Anhefte-Menü im Composer (ersetzt die einzelnen 📎-/📷-Knöpfe; künftige
    # Composer-Aktionen hier eintragen statt die Eingabezeile zu vergrößern).
    "chat.attach_menu": "Anhängen",
    "chat.am_camera": "Foto aufnehmen",
    "chat.am_gallery": "Foto aus der Galerie",
    "chat.am_proj_into": "Uploads \u2192 Projekt",
    "chat.am_proj_hint": "Uploads in den verknüpften Projektordner senden, statt sie an diesen Chat anzuhängen",
    # Verknüpfter Projektordner (DEC-049) — Kopfleiste + Upload-Ziel im Composer.
    "chat.proj_into": "\u2192 Projekt",
    "chat.proj_clean": "Verknüpftes Projekt: keine ausstehenden Änderungen (klik zum Aktualisieren)",
    "chat.proj_pending": "Ausstehende Projektänderungen \u2014 klik zur Prüfung und Übernahme",
    "chat.proj_conflict": "Änderungen gesperrt: Dateien wurden im echten Ordner seit dem Import geändert. Neu importieren oder eigene Bearbeitungen verwerfen (Admin \u2192 Projekt).",
    "chat.proj_err": "Verknüpftes Projekt konnte nicht gelesen werden",
    "chat.proj_review": "Projektänderungen",
    "chat.proj_apply": "Auf Ordner anwenden",
    "chat.proj_writing": "Schreibe\u2026",
    "chat.proj_blocked": "Gesperrt \u2014 diese Dateien wurden im echten Ordner seit dem Import geändert:",
    "chat.proj_blocked_hint": "Neu importieren (Admin \u2192 Projekt), um die Version des Ordners zu übernehmen, oder eigene Änderungen rückgängig machen. Bei Konflikten wird nichts angewendet.",
    "chat.proj_disabled": "Anwenden ist auf diesem System nicht verfügbar (kein Schreib-Backend, oder Verschlüsselung im Ruhezustand ist aktiv).",
    "chat.proj_nothing": "Nichts anzuwenden.",
    "chat.proj_confirm": "Diese Änderungen in den echten Ordner schreiben?",
    "chat.proj_confirm2": "Überschriebene Dateien werden vorher gesichert.",
    "chat.proj_backup": "Sicherung",
    "chat.proj_failed": "Anwenden fehlgeschlagen.",
    "chat.drop_here": "Dateien zum Hochladen hierher ziehen",
    "chat.camera": "Kamera / Foto",
    "chat.device_allow": "Gerätezugriff erlauben",
    "chat.device_granted": "Gerätezugriff: erlaubt",
    "chat.device_note": "Dateien anhängen oder herunterladen geht immer ohne diesen Schalter — das ist deine eigene Dateiübertragung, kein Gerätezugriff.",
    "chat.device_hint": "Ein Geräte-Helfer ist gekoppelt. Geräte-Werkzeuge (Ordnerliste, Screenshots) brauchen Zugriff je Chat — frag einen Admin nach Details.",
    "chat.abort": "Stopp",
    "chat.stop": "Stopp",
    "chat.aborting": "Stoppe…",
    "chat.aborted": "Umlauf abgebrochen.",
    "chat.retry": "↻ Wiederholen",
    "chat.retrying": "wiederhole…",
    "chat.force_respond": "⚡ Antwort erzwingen",
    "chat.force_armed": "⚡ nächste Antwort ohne Werkzeuge",
    "chat.agent_mode": "🤖 Agent-Modus: an",
    "chat.agent_mode_on": "Agent-Modus: an",
    "chat.chat_only": "Nur Chat (ohne Werkzeuge)",
    "chat.thinking": "denke nach…",
    "chat.generating": "wird erstellt",
    "chat.writing": "schreibe",
    "chat.composing_cmd": "Befehl wird erstellt",
    "chat.await_approval": "wartet auf Freigabe",
    "chat.still_running": "in diesem Chat läuft noch ein Umlauf — Stopp beendet ihn",
    "chat.ctx_title": "Vom Modell in der letzten Runde empfangene Tokens / konfigurierter Kontextfenster",
    "chat.ctx_est": "geschätzt",
    "chat.ctx_warn": "Kontext fast voll: neuen Chat starten oder Dateien kürzen.",
    "chat.download": "Herunterladen",
    # Files panel: the filename is the download link, everything else is in the ⋮ menu.
    "chat.art_open_dl": "Herunterladen (oder ⋮ für mehr Optionen)",
    "chat.art_attach": "An Nachricht anhängen",
    "chat.art_attached": "An die nächste Nachricht angehängt.",
    "chat.art_already_attached": "Bereits angehängt.",
    "chat.art_preview": "Vorschau öffnen",
    "chat.art_copy_link": "Link kopieren",
    "chat.art_link_copied": "Link kopiert.",
    "chat.art_copy_failed": "Link konnte nicht kopiert werden.",
    "chat.art_delete_confirm": "Diese Datei löschen? Sie wird auch aus deinem Arbeitsbereich entfernt.",
    "chat.art_delete_failed": "Datei konnte nicht gelöscht werden.",
    "chat.intro_head": "So funktionieren Dateien hier:",
    "chat.intro_body": "Bitte um eine Datei – der Assistent schreibt sie in deinen eigenen Arbeitsbereich; alle Dateien bleiben unter „Dateien“ aufgelistet (dieser Chat / alle), inklusive Download. Einen eigenen ⬇-Download-Button im Gespräch gibt es nur, wenn du danach fragst. Beim Schreiben läuft die Fortschrittsanzeige live mit.",
    "chat.intro_dismiss": "Verstanden",
    "chat.approve": "Erlauben",
    "chat.deny": "Ablehnen",
    "chat.reply": "Antworten",
    "chat.tool": "Werkzeug",
    "chat.media_fullscreen": "Vollbild",
    "chat.media_download": "Herunterladen",
    "chat.media_unsupported": "Dein Browser kann diesen Dateityp nicht abspielen.",
    "chat.media_missing": "Diese Datei liegt nicht mehr in deinem Arbeitsbereich.",
    "chat.web_sources": "Quellen",
    "chat.web_read": "Seite gelesen",
    "chat.web_mode": "über",
    "chat.prefill": "Vorverarbeitung",
    # Audio-Beschreiber: erscheint, während ein zweites Modell eine angehängte Aufnahme
    # anhört. Das dauert real (ffmpeg + Zuhören) und darf nicht wie ein Hänger aussehen.
    "chat.listening": "höre mir das Audio an…",
    "chat.caption_failed": "Ich konnte mir diese Datei nicht anhören — der Hinweis in meiner Nachricht sagt warum.",
    # Kopfzeile des ausklappbaren Blocks mit den Worten des Audio-Modells beim Mithören.
    "chat.caption_streaming": "Audio-Modell",
    # Erscheint bei wiederhergestellten Beschreibungen, die fehlgeschlagen sind — damit ein
    # alter Fehler nach einem Neuladen nicht als fehlendes Feature gelesen wird (DEC-061).
    "chat.caption_failed_short": "Konnte sie nicht beschreiben",

    # Speed labels for the measured rates llama.cpp reports (see hearth/perf.py). The UI
    # shows these in preference to Hearth's own wall-clock tok/s estimate.
    "chat.decode": "Generierung",
    # Collapsible header for the model's thinking block (live and in history).
    "chat.reasoning": "Gedankengang",
    # Steering queue row (message sent while a turn owns the transcript).
    "chat.steer_queued": "Für den nächsten Schritt eingereiht",
    "chat.steer_withdraw": "Zurückziehen",
    "chat.steer_hint": "Ein Umlauf läuft — die Nachricht wird zwischen den Tool-Schritten gelesen.",
    # ETA suffix on the live prefill label ("Vorverarbeitung 43 % · noch ≈1m 20s").
    "chat.left": "noch",
    "chat.protocol": "Protokoll",
    "chat.menu": "Menü",
    "chat.close_menu": "Menü schließen",
    "chat.more_options": "Weitere Optionen",
    "nav.chats": "Chats",
    "nav.files": "Dateien",
    "nav.prompt": "System-Prompt",
    "nav.skills": "Skills",
    "nav.chats_short": "Chats",
    "nav.files_short": "Datei",
    "nav.prompt_short": "Prompt",
    "nav.skills_short": "Skills",
    "nav.settings_short": "Setup",
    "prompt.title": "System-Prompt",
    "prompt.hint": "Das System-Prompt für jeden Chat, ab der nächsten Nachricht. Direkt bearbeitbar.",
    "prompt.save": "Speichern",
    "prompt.saved": "Gespeichert ✓",
    "prompt.save_failed": "Speichern fehlgeschlagen",
    "prompt.reset": "Leeren",
    "prompt.reset_title": "Persönlichen Prompt entfernen und nur die eingebaute Persona verwenden",
    "prompt.placeholder_base": "Deine Anweisungen hier eintragen…",
    "skills.title": "Skills",
    "skills.hint": "Wiederverwendbare Instruktionsformate, die ein Admin bereitstellt.",
    "skills.none": "Noch nichts hier.",
    "settings.title": "Einstellungen",
    "settings.enter_sends": "Enter sendet die Nachricht",
    "settings.enter_sends_hint": "An: Enter = senden, Umschalt+Enter = neue Zeile. Aus: Enter = neue Zeile, Senden per Button (Strg/Cmd+Enter sendet ebenfalls).",
    "settings.auto_scroll": "Automatisch zur neuesten Nachricht scrollen",
    "settings.auto_scroll_hint": "An: Das Gespräch folgt der Ausgabe beim Streaming. Aus: Es bleibt, wo du es hingesrollt hast.",
    "settings.timestamps": "Zeitstempel an Nachrichten zeigen",
    "settings.timestamps_hint": "Kleine Zeitangabe an jeder Blase (lokale Browserzeit).",
    "settings.debug_hint": "Das Roh-Ereignisprotokoll unter dem Chat anzeigen.",
    "login.title": "Anmelden · Hearth",
    "login.heading": "Herdfeuer",
    "login.subtitle": "Privates Familien-LLM. Bitte anmelden.",
    "login.username": "Benutzername",
    "login.password": "Passwort",
    "login.totp": "2FA-Code",
    "login.button": "Anmelden",
    "login.note": "Der Zugriff ist auf eingeladene Konten beschränkt. Falls du diese Seite versehentlich erreicht hast, schließe sie bitte.",
    "login.failed": "Anmeldung fehlgeschlagen",
    "common.language": "Sprache",
}

_TABLES = {"en": _EN, "de": _DE}


def normalize(lang: str | None) -> str:
    if not lang:
        return DEFAULT_LANG
    lang = str(lang).strip().lower()[:2]
    return lang if lang in SUPPORTED else DEFAULT_LANG


def t(key: str, lang: str = DEFAULT_LANG) -> str:
    lang = normalize(lang)
    return _TABLES[lang].get(key) or _EN.get(key) or key


def dict_for(lang: str) -> dict:
    lang = normalize(lang)
    merged = dict(_EN)
    merged.update(_TABLES[lang])
    return merged
