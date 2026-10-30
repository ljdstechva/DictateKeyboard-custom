#!/usr/bin/env python3
"""Live emulator test for the custom build's offline switch, transcribing animation and Backspace hold.

Runs on a GitHub-hosted runner next to a booted emulator (see .github/workflows/live-test.yml):

  * the APK is installed, set as the keyboard and pointed at a mock OpenAI-compatible server on the
    runner (tools/livetest/mock_stt_server.py, reached from the emulator as 10.0.2.2);
  * speech is a TTS WAV streamed into the emulator microphone over gRPC (tools/inject_emulator_audio.py,
    40 ms packets: the emulator's injection buffer holds only ~300 ms), and only while the app is
    recording. --audio pulse plays it into a PulseAudio sink instead, for a desktop emulator started
    with -allow-host-audio (on GitHub's runners the emulator cannot open PulseAudio). Scenarios A and C
    need no speech and record silence, so a failing injection cannot spoil them;
  * dictation is started and stopped by tapping the keyboard's mic, exactly as a user would. A tap only
    counts when the app logs phase=captureStarted / phase=stopTapped.

Scenarios (the mock decides how slow "online" is):

  A  online answers after 3 s                -> online text is typed, no offline switch, probe answered
  E  Backspace on A's text: tap, then hold   -> a tap deletes one character, a hold deletes whole words
  C  no internet (nothing answers), no model -> request cancelled at ~5 s, "download an offline model" error
  D  server reachable, uploads dropped       -> offline model takes over after the call's retries (~10 s)
  B  online answers after 12 s, model ready  -> no switch: the provider is reachable, its text is typed
  F  no internet (nothing answers), model    -> request cancelled at ~5 s, offline model types the text
  U  Settings > Updates > Check for updates  -> offers the highest release tag on GitHub (not the first listed)
  G  flight mode, model present              -> offline model at once (no network), no 5 s wait

Everything lands in --out: screenshots, logcat per scenario, UI dumps, mock request log, summary.json.
Exit code 0 only when every scenario passed.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

PKG = "net.devemperor.dictate.bench"
IME = f"{PKG}/dev.patrickgold.florisboard.FlorisImeService"
ACTIVITY = f"{PKG}/dev.patrickgold.florisboard.app.FlorisAppActivity"
PREFS = f"/data/data/{PKG}/jetpref_datastore/florisboard-app-prefs.jetpref"
MODELS_DIR = f"/data/data/{PKG}/files/dictate-models"
MODEL_ID = "whisper-tiny.en"
CUSTOM_ID = "custom:livetest"
MOCK = "http://127.0.0.1:8080"
ONLINE_TEXT = "Mock online transcript"
HINT_TEXT = "Try out your setup"
REPO = os.environ.get("GITHUB_REPOSITORY", "ljdstechva/DictateKeyboard-custom")
RELEASE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)-custom\.(\d+)")
# Smartbar mic on the pixel_6 profile (1080x2400): measured from a CI screenshot of this build first,
# then upstream's own benchmark position (tools/benchmark_dictation_latency.ps1), then a little higher.
MIC_CANDIDATES = [(1020, 1574), (1015, 1625), (1020, 1545)]
# Backspace of the modern keyboard on the same profile (right end of the z row), measured from the
# A-online-result screenshot of run 40883657387. The rows are anchored to the bottom, so the smartbar
# shifting the mic does not move it; scenario E checks it with one tap before holding it.
BACKSPACE = (990, 2050)
# A hold this long gives two word deletes (the first after the 300 ms long-press delay, then one per
# 250 ms); deleting characters instead would take about 8 and stop inside a word.
BACKSPACE_HOLD_MS = 700

ROOT = Path(__file__).resolve().parents[2]
OUT = Path("livetest-out")
results = []


def log(msg):
    print(f"[live-test {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def adb(*args, check=True, timeout=120, binary=False):
    proc = subprocess.run(["adb", *args], capture_output=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)} failed: {proc.stderr.decode(errors='replace')}")
    return proc.stdout if binary else proc.stdout.decode(errors="replace")


def sh(cmd, check=True, timeout=120):
    return adb("shell", cmd, check=check, timeout=timeout)


def mock(path):
    with urllib.request.urlopen(MOCK + path, timeout=10) as resp:
        return json.loads(resp.read())


def screenshot(name):
    data = adb("exec-out", "screencap", "-p", binary=True, timeout=30)
    path = OUT / f"{name}.png"
    path.write_bytes(data)
    log(f"screenshot {path.name}")
    return path


def dump_ui(name=None):
    """uiautomator dump of every window on screen (app, keyboard, system bars). Retries: it needs idle."""
    for attempt in range(4):
        out = sh("uiautomator dump /sdcard/ui.xml", check=False, timeout=40)
        if "dumped to" in out:
            xml = adb("exec-out", "cat", "/sdcard/ui.xml", timeout=30)
            if name:
                (OUT / f"{name}.xml").write_text(xml)
            return xml
        time.sleep(1.5)
    log("uiautomator dump failed: " + out.strip())
    return ""


def nodes(xml):
    if not xml:
        return []
    try:
        return list(ET.fromstring(xml).iter("node"))
    except ET.ParseError:
        return []


def center(node):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def tap(xy):
    sh(f"input tap {xy[0]} {xy[1]}")


def field_node(xml):
    for n in nodes(xml):
        if n.get("package") == PKG and "EditText" in n.get("class", ""):
            return n
    for n in nodes(xml):
        if n.get("text") == HINT_TEXT:
            return n
    return None


def field_text():
    n = field_node(dump_ui())
    if n is None:
        return None
    text = n.get("text", "")
    return "" if text == HINT_TEXT else text


def keyboard_shown():
    return "mInputShown=true" in sh("dumpsys input_method", check=False)


def logcat_clear():
    adb("logcat", "-c", check=False)


def logcat_save(name):
    text = adb("logcat", "-d", "-v", "time", check=False, timeout=60)
    (OUT / f"{name}-logcat-full.txt").write_text(text)
    keep = [line for line in text.splitlines()
            if re.search(r"DictateLatency|DictateHTTP|AndroidRuntime|FATAL|sherpa|Dictate", line)]
    (OUT / f"{name}-logcat.txt").write_text("\n".join(keep) + "\n")
    return text


def wait_for(predicate, timeout, step=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def latency_lines():
    return adb("logcat", "-d", "-s", "DictateLatency:I", check=False, timeout=60)


# --- app setup ------------------------------------------------------------------------------------

def encode(value):
    return '"' + (value.replace("\\", "\\\\").replace("\r", "\\r")
                  .replace("\n", "\\n").replace('"', '\\"')) + '"'


def app_uid():
    return sh(f"stat -c %u /data/data/{PKG}").strip()


def seed_prefs(extra):
    """Rewrite the app's jetpref datastore (lines `type;key;value`) with the test settings. App stopped."""
    sh(f"am force-stop {PKG}")
    raw = sh(f"cat {PREFS}", check=False)
    entries = {}
    for line in raw.splitlines():
        parts = line.split(";", 2)
        if len(parts) == 3:
            entries[parts[1]] = (parts[0], parts[2])
    (OUT / "prefs-before.txt").write_text(redact(raw))
    entries.update(extra)
    content = "".join(f"{t};{k};{v}\n" for k, (t, v) in entries.items())
    local = OUT / "prefs.jetpref"
    local.write_text(content)
    adb("push", str(local), "/data/local/tmp/prefs.jetpref")
    uid = app_uid()
    sh(f"mkdir -p /data/data/{PKG}/jetpref_datastore && cp /data/local/tmp/prefs.jetpref {PREFS} && "
       f"chown -R {uid}:{uid} /data/data/{PKG}/jetpref_datastore && chmod 600 {PREFS} && "
       f"restorecon -R /data/data/{PKG}/jetpref_datastore && rm /data/local/tmp/prefs.jetpref")
    local.unlink()
    (OUT / "prefs-seeded.txt").write_text(redact(content))


def redact(text):
    return re.sub(r'(\\"apiKey\\":\\")[^\\]*', r"\1***", text)


def install_model(model_dir):
    sh(f"am force-stop {PKG}")
    target = f"{MODELS_DIR}/{MODEL_ID}"
    sh(f"mkdir -p {target}")
    for f in sorted(Path(model_dir).iterdir()):
        adb("push", str(f), f"/data/local/tmp/{f.name}", timeout=300)
        sh(f"mv /data/local/tmp/{f.name} {target}/{f.name}")
    uid = app_uid()
    sh(f"chown -R {uid}:{uid} {MODELS_DIR} && restorecon -R {MODELS_DIR}")
    log("model files on device: " + sh(f"ls -l {target}").replace("\n", " | "))


def enable_keyboard():
    """Right after install the input method list can lag behind; wait until the system knows the IME."""
    ime_id = None
    for _ in range(30):
        listed = sh("ime list -a -s", check=False)
        ime_id = next((line.strip() for line in listed.splitlines()
                       if line.strip().startswith(PKG + "/") and "FlorisImeService" in line), None)
        if ime_id:
            break
        time.sleep(2)
    (OUT / "ime-list.txt").write_text(sh("ime list -a", check=False))
    if not ime_id:
        raise RuntimeError("the keyboard never appeared in `ime list -a`")
    sh(f"ime enable {ime_id}")
    sh(f"ime set {ime_id}")
    IME_STATE["id"] = ime_id
    log(f"keyboard enabled and selected: {ime_id}")


IME_STATE = {}


def current_keyboard():
    return sh("settings get secure default_input_method", check=False).strip()


def ensure_keyboard_selected():
    """Force-stopping the app makes Android fall back to Gboard; select ours again until it sticks."""
    want = IME_STATE.get("id")
    if not want:
        return
    current = current_keyboard()
    if current == want:
        return
    log(f"default keyboard was {current!r}, selecting ours again")
    for _ in range(10):
        sh(f"ime set {want}", check=False)
        if wait_for(lambda: current_keyboard() == want, 3):
            return
    raise RuntimeError(f"could not select the keyboard again (still {current_keyboard()!r})")


def dismiss_system_dialogs():
    """CI emulators often show "<app> isn't responding" on top; answer it with Wait."""
    for _ in range(4):
        ns = nodes(dump_ui())
        if not ns or ns[0].get("package") != "android":
            return
        button = next((n for n in ns if n.get("text") == "Wait"), None) or \
            next((n for n in ns if n.get("text") in ("OK", "Close app", "Close")), None)
        if button is None:
            return
        title = next((n.get("text") for n in ns if n.get("text")), "")
        log(f"dismissing system dialog {title!r} with {button.get('text')!r}")
        tap(center(button))
        time.sleep(2)


def open_home():
    sh(f"am force-stop {PKG}")
    time.sleep(1.5)  # the system's own fallback to another keyboard lands shortly after the kill
    ensure_keyboard_selected()
    sh(f"am start -W -n {ACTIVITY}")
    time.sleep(4)
    dismiss_system_dialogs()
    ensure_keyboard_selected()


def open_keyboard_id():
    shown = re.search(r"\b(?:mCurMethodId|mSelectedMethodId|mCurId)=(\S+)", sh("dumpsys input_method", check=False))
    return shown.group(1) if shown else None


def focus_field(tag):
    """Tap the try-out field until our keyboard (not Gboard) is the one that opens.

    Right after the app was force-stopped, Android can still bind its fallback keyboard for a moment even
    though ours is selected again, so a wrong keyboard is closed and the field focused once more."""
    for attempt in range(1, 4):
        xml = dump_ui(f"{tag}-home")
        n = field_node(xml)
        if n is None:
            screenshot(f"{tag}-no-field")
            raise RuntimeError("try-out field not found on the home screen")
        tap(center(n))
        if not wait_for(keyboard_shown, 10):
            raise RuntimeError("keyboard did not open on the try-out field")
        time.sleep(2.5)
        current = open_keyboard_id()
        if current is None:
            log("warning: dumpsys input_method names no current keyboard")
            return
        if current.startswith(PKG + "/"):
            return
        log(f"attempt {attempt}: the open keyboard is {current}, not ours; closing it and selecting ours")
        sh("input keyevent KEYCODE_BACK", check=False)
        time.sleep(1)
        ensure_keyboard_selected()
        time.sleep(2)
    raise RuntimeError(f"the open keyboard is not ours: {open_keyboard_id()}")


def phase_count(phase):
    return latency_lines().count(f"phase={phase}")


def start_recording(tag, cache):
    """Tap the smartbar mic; a spot only counts when the app logs that capture started."""
    candidates = [cache["mic"]] if "mic" in cache else MIC_CANDIDATES
    for xy in candidates:
        before = phase_count("captureStarted")
        tap(xy)
        if wait_for(lambda: phase_count("captureStarted") > before, 6):
            if "mic" not in cache:
                log(f"mic at {xy} started the recording")
            cache["mic"] = xy
            return xy
        screenshot(f"{tag}-mic-miss-{xy[0]}-{xy[1]}")
        log(f"tap at {xy} did not start a recording")
    raise RuntimeError("no mic tap started a recording")


def play_audio(wav, stubs, tag):
    if AUDIO["mode"] == "pulse":
        proc = subprocess.run(["paplay", f"--device={AUDIO['sink']}", str(wav)],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"paplay failed: {proc.stderr.strip()}")
        return
    sent = OUT / f"{tag}.sent"
    done = OUT / f"{tag}.done"
    errors = OUT / f"{tag}-inject.txt"
    with open(errors, "w") as err:
        proc = subprocess.Popen([sys.executable, str(ROOT / "tools/inject_emulator_audio.py"), str(wav),
                                 "--stubs", str(stubs), "--packet-ms", "40",
                                 "--sent-marker", str(sent), "--completed-marker", str(done)],
                                stdout=err, stderr=subprocess.STDOUT)
    try:
        if not wait_for(lambda: sent.exists() or proc.poll() is not None, 40, 0.1) or not sent.exists():
            raise RuntimeError("audio injection failed: " + errors.read_text()[-400:].strip())
    finally:
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        for marker in (sent, done):
            marker.unlink(missing_ok=True)


AUDIO = {"mode": "grpc", "sink": "virtmic"}


def record(cache, wav, stubs, tag, speech=True):
    """Start dictation with the mic, put the speech into the microphone (or record ~4 s of silence),
    stop with the mic. Returns the monotonic time of the stop tap."""
    mic = start_recording(tag, cache)
    time.sleep(0.6)
    screenshot(f"{tag}-recording")
    if speech:
        play_audio(wav, stubs, tag)
    else:
        time.sleep(4)
    time.sleep(0.4)
    before = phase_count("stopTapped")
    tap(mic)
    stopped = time.monotonic()
    if not wait_for(lambda: phase_count("stopTapped") > before, 5, 0.2):
        raise RuntimeError("the second mic tap did not stop the recording")
    log("recording stopped")
    return stopped


def check(scenario, name, ok, detail=""):
    results.append({"scenario": scenario, "check": name, "ok": bool(ok), "detail": detail})
    log(f"{'PASS' if ok else 'FAIL'} [{scenario}] {name} {detail}")


def offline_switch_line(text):
    m = re.search(r"phase=offlineSwitch reason=(\S+) sinceTranscribingMs=(\d+) model=(\S+) used=(\S+)", text)
    return m.groups() if m else None


SPOKEN_WORDS = ("hello", "live", "test", "offline", "switch", "quick", "brown", "fox", "jumps", "lazy", "dog")


def spoken_words(text):
    """How many words of the played sentence the offline model wrote: proof real speech went in.

    whisper-tiny hears espeak's robotic voice only roughly (2-4 of these in CI, sometimes followed by a
    repetition loop); silence gives 0."""
    found = set(re.findall(r"[a-z]+", (text or "").lower()))
    return sum(1 for w in SPOKEN_WORDS if w in found)


def heard_speech(text):
    # Two words of the sentence cannot come from silence (0 words) or a hallucination loop alone.
    # Not "Hello" in particular: run 40918258149 heard "It is the life that the flying switch, the quick
    # round of ice jump so perfectly gnar." for it.
    return spoken_words(text) >= 2


def last_request():
    reqs = mock("/requests")
    return reqs[-1] if reqs else {}


# --- scenarios ------------------------------------------------------------------------------------

def scenario_online(mic_cache, wav, stubs):
    tag = "A-online"
    mock("/control?delay=3&mode=normal")
    open_home()
    focus_field(tag)
    screenshot(f"{tag}-keyboard")
    logcat_clear()
    stopped = record(mic_cache, wav, stubs, tag, speech=False)
    time.sleep(max(0, 1.2 - (time.monotonic() - stopped)))
    screenshot(f"{tag}-transcribing")
    text = wait_for(lambda: (t := field_text()) and ONLINE_TEXT in t and t, 25, 1.0) or field_text()
    screenshot(f"{tag}-result")
    lat = latency_lines()
    logcat_save(tag)
    check(tag, "online text typed into the field", text and ONLINE_TEXT in text, repr(text))
    check(tag, "no offline switch", "phase=offlineSwitch" not in lat)
    req = last_request()
    check(tag, "mock answered", req.get("outcome") == "answered", json.dumps(req))
    probes = mock("/probes")
    check(tag, "reachability probe answered", probes.get("answered", 0) >= 1, json.dumps(probes))


def ends_on_word(before, after):
    """True when `after` is `before` with whole words taken off its end, as a word delete leaves it."""
    kept = after.split()
    return (len(kept) < len(before.split()) and before.split()[:len(kept)] == kept
            and before.startswith(after) and (after == "" or after[-1].isspace()))


def scenario_hold_backspace(mic_cache, wav, stubs):
    """Runs straight after A, on the online text A left in the field (the keyboard is still open)."""
    tag = "E-hold-backspace"
    start = field_text()
    if not start or ONLINE_TEXT not in start:
        raise RuntimeError(f"no text from scenario A to delete: {start!r}")
    tap(BACKSPACE)
    time.sleep(1.5)
    tapped = field_text() or ""
    check(tag, "a tap deletes one character", tapped == start[:-1], f"{start!r} -> {tapped!r}")
    texts = [tapped]
    for hold in (1, 2):
        sh(f"input swipe {BACKSPACE[0]} {BACKSPACE[1]} {BACKSPACE[0]} {BACKSPACE[1]} {BACKSPACE_HOLD_MS}")
        time.sleep(1.5)
        texts.append(field_text() or "")
        screenshot(f"{tag}-hold{hold}")
        before, after = texts[-2], texts[-1]
        gone = len(before.split()) - len(after.split())
        # Two holds: one could stop on a space by chance even deleting characters, both hardly.
        check(tag, f"hold {hold} deletes whole words", ends_on_word(before, after) and gone >= 1 and after.strip(),
              f"{before!r} -> {after!r} ({gone} words)")
    logcat_save(tag)


def newest_release_tag():
    """(highest release tag by number, the tag GitHub lists first) for releases carrying an APK."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "dictate-livetest"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases?per_page=100", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        rels = [r for r in json.load(resp)
                if not r["draft"] and any(a["name"].endswith(".apk") for a in r["assets"])]
    tagged = [(tuple(map(int, m.groups())), r["tag_name"]) for r in rels
              if (m := RELEASE_TAG.fullmatch(r["tag_name"]))]
    return max(tagged)[1], rels[0]["tag_name"]


def node_with_text(xml, prefix, exact=False):
    # Compare with None, never by truth: an ElementTree node without children is falsy.
    for n in nodes(xml):
        text = n.get("text", "")
        if text == prefix or (not exact and text.startswith(prefix)):
            return n
    return None


def text_center(text):
    # Exact: "Check for updates" is also how the auto-check switch's title starts (run 40922340518
    # tapped that switch instead of the button).
    n = node_with_text(dump_ui(), text, exact=True)
    return center(n) if n is not None else None


def scenario_updater(mic_cache, wav, stubs):
    """The test build's tag (livetest-N) is not a release tag, so the updater offers whatever it
    takes for the newest release: that has to be the highest number, not the first in GitHub's list."""
    tag = "U-updater"
    expected, listed_first = newest_release_tag()
    log(f"newest release {expected}; GitHub lists {listed_first} first")
    sh(f"am force-stop {PKG}")
    time.sleep(1.5)
    # BROWSABLE is what makes the app treat a VIEW intent as a deep link; without it the URI is taken
    # for an extension file and "Import extension" opens.
    sh(f"am start -W -a android.intent.action.VIEW -c android.intent.category.BROWSABLE "
       f"-d ui://florisboard/settings/custom-updates -n {ACTIVITY}")
    time.sleep(4)
    dismiss_system_dialogs()
    # Wait until the button is enabled again: opening the screen starts a check of its own.
    time.sleep(3)
    button = wait_for(lambda: text_center("Check for updates"), 30, 1.0)
    if button is None:
        screenshot(f"{tag}-no-button")
        raise RuntimeError("Updates screen not shown")
    tap(button)
    xml = ""

    def offered():
        nonlocal xml
        xml = dump_ui()
        n = node_with_text(xml, "Update available:")
        return n.get("text") if n is not None else None

    shown = wait_for(offered, 40, 2.0)
    screenshot(f"{tag}-result")
    (OUT / f"{tag}-result.xml").write_text(xml)
    status = [n.get("text") for n in nodes(xml) if n.get("package") == PKG and n.get("text")]
    check(tag, "offers the highest release tag", shown == f"Update available: {expected}",
          f"{shown!r}; newest {expected}, listed first {listed_first}" + ("" if shown else f"; screen {status}"))


def scenario_no_model(mic_cache, wav, stubs):
    tag = "C-no-internet-no-model"
    mock("/control?delay=0&mode=blackhole")
    open_home()
    focus_field(tag)
    logcat_clear()
    stopped = record(mic_cache, wav, stubs, tag, speech=False)
    time.sleep(max(0, 3.0 - (time.monotonic() - stopped)))
    screenshot(f"{tag}-transcribing-3s")
    line = wait_for(lambda: offline_switch_line(latency_lines()), 20, 0.5)
    time.sleep(1.5)
    screenshot(f"{tag}-error")
    time.sleep(2)
    lat = latency_lines()
    logcat_save(tag)
    text = field_text()
    check(tag, "offline switch fired: no internet", line and line[0] == "noInternet", repr(line))
    if line:
        ms = int(line[1])
        check(tag, "switch at ~5 s", 4500 <= ms <= 7500, f"{ms} ms")
        check(tag, "no model reported", line[2] == "none" and line[3] == "false", repr(line))
    check(tag, "error shown (apiError TIMEOUT)", re.search(r"phase=apiError kind=TIMEOUT", lat), "")
    check(tag, "field stays empty", not text, repr(text))
    req = last_request()
    check(tag, "online request cancelled by the app", req.get("outcome") == "client-closed" and
          req.get("afterSeconds", 99) <= 7.5, json.dumps(req))
    mock("/control?mode=normal")


def scenario_network_error(mic_cache, wav, stubs):
    tag = "D-network-error-model"
    mock("/control?delay=0&mode=reset")
    open_home()
    focus_field(tag)
    logcat_clear()
    record(mic_cache, wav, stubs, tag)
    line = wait_for(lambda: offline_switch_line(latency_lines()), 30, 0.5)
    text = wait_for(lambda: field_text(), 90, 2.0)
    screenshot(f"{tag}-result")
    logcat_save(tag)
    # The server is reachable (the probe is answered) but drops every upload: no-internet does not
    # apply, the call's own network error after its retries (3 s apart) hands over.
    check(tag, "offline model used after a network error",
          line and line[0] == "NETWORK" and line[3] == "true", repr(line))
    if line:
        check(tag, "handover within ~13 s", int(line[1]) <= 13000, f"{line[1]} ms")
    check(tag, "offline text typed", text and ONLINE_TEXT not in text, repr(text))
    check(tag, "offline text is the spoken sentence", heard_speech(text), f"{spoken_words(text)} words")
    mock("/control?mode=normal")


def scenario_slow_with_model(mic_cache, wav, stubs):
    """The owner's bug: internet works, the provider is slow, an offline model is ready. Wait online."""
    tag = "B-slow-online-with-model"
    mock("/control?delay=12&mode=normal")
    open_home()
    focus_field(tag)
    logcat_clear()
    stopped = record(mic_cache, wav, stubs, tag)
    time.sleep(max(0, 8.0 - (time.monotonic() - stopped)))
    screenshot(f"{tag}-still-online-8s")
    text = wait_for(lambda: (t := field_text()) and ONLINE_TEXT in t and t, 40, 1.0) or field_text()
    screenshot(f"{tag}-result")
    lat = latency_lines()
    logcat_save(tag)
    check(tag, "no offline switch while the provider is reachable", "phase=offlineSwitch" not in lat,
          repr(offline_switch_line(lat)))
    check(tag, "online text typed after the slow answer", text and ONLINE_TEXT in text, repr(text))
    req = last_request()
    check(tag, "mock answered after its 12 s delay", req.get("outcome") == "answered" and
          req.get("afterSeconds", 0) >= 11.5, json.dumps(req))
    probes = mock("/probes")
    check(tag, "reachability probe answered", probes.get("answered", 0) >= 1, json.dumps(probes))
    mock("/control?delay=0")


def scenario_no_internet_with_model(mic_cache, wav, stubs):
    tag = "F-no-internet-with-model"
    mock("/control?delay=0&mode=blackhole")
    open_home()
    focus_field(tag)
    logcat_clear()
    record(mic_cache, wav, stubs, tag)
    line = wait_for(lambda: offline_switch_line(latency_lines()), 20, 0.3)
    time.sleep(0.8)
    screenshot(f"{tag}-offline")
    text = wait_for(lambda: field_text(), 120, 2.0)
    screenshot(f"{tag}-result")
    logcat_save(tag)
    check(tag, "offline switch fired: no internet", line and line[0] == "noInternet", repr(line))
    if line:
        ms = int(line[1])
        check(tag, "switch at ~5 s", 4500 <= ms <= 7500, f"{ms} ms")
        check(tag, "downloaded model used", line[2] == MODEL_ID and line[3] == "true", repr(line))
    check(tag, "offline text typed (not the online text)", text and ONLINE_TEXT not in text, repr(text))
    check(tag, "offline text is the spoken sentence", heard_speech(text), f"{spoken_words(text)} words")
    req = last_request()
    check(tag, "online request cancelled by the app", req.get("outcome") == "client-closed" and
          req.get("afterSeconds", 99) <= 7.5, json.dumps(req))
    mock("/control?mode=normal")


def scenario_no_network_with_model(mic_cache, wav, stubs):
    """Flight mode: the phone knows it has no network, so the switch does not wait at all."""
    tag = "G-no-network-with-model"
    mock("/control?delay=0&mode=normal")
    open_home()
    focus_field(tag)
    sh("cmd connectivity airplane-mode enable")
    try:
        time.sleep(4)
        logcat_clear()
        record(mic_cache, wav, stubs, tag)
        line = wait_for(lambda: offline_switch_line(latency_lines()), 20, 0.3)
        text = wait_for(lambda: field_text(), 120, 2.0)
        screenshot(f"{tag}-result")
        logcat_save(tag)
    finally:
        sh("cmd connectivity airplane-mode disable", check=False)
    check(tag, "offline switch fired: no network", line and line[0] == "noNetwork", repr(line))
    if line:
        check(tag, "switch without waiting", int(line[1]) <= 2000, f"{line[1]} ms")
        check(tag, "downloaded model used", line[2] == MODEL_ID and line[3] == "true", repr(line))
    check(tag, "offline text typed", text and ONLINE_TEXT not in text, repr(text))
    check(tag, "offline text is the spoken sentence", heard_speech(text), f"{spoken_words(text)} words")


def make_stubs(dest):
    import grpc_tools
    proto_dir = Path(os.environ.get("ANDROID_HOME", os.environ.get("ANDROID_SDK_ROOT", ""))) / "emulator/lib"
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "grpc_tools.protoc", f"-I{proto_dir}",
                    f"-I{Path(grpc_tools.__file__).parent / '_proto'}",
                    f"--python_out={dest}", f"--grpc_python_out={dest}",
                    str(proto_dir / "emulator_controller.proto")], check=True)


def main():
    global OUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--apk", required=True)
    parser.add_argument("--wav", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--out", default="livetest-out")
    parser.add_argument("--audio", choices=["pulse", "grpc"], default="grpc")
    parser.add_argument("--pulse-sink", default="virtmic")
    args = parser.parse_args()
    OUT = Path(args.out)
    OUT.mkdir(parents=True, exist_ok=True)
    AUDIO.update(mode=args.audio, sink=args.pulse_sink)
    stubs = OUT.parent / "grpc-stubs"
    if args.audio == "grpc":
        make_stubs(stubs)

    log("device: " + sh("getprop ro.build.version.release").strip() + " / " + sh("wm size").strip())
    # Let the freshly booted system settle (the launcher is slow under software rendering).
    sh("settings put global hide_error_dialogs 1", check=False)
    time.sleep(20)
    dismiss_system_dialogs()
    adb("install", "-r", "-g", args.apk, timeout=300)
    enable_keyboard()
    # First start writes the datastore; then the test settings are put in as root.
    sh(f"am start -W -n {ACTIVITY}")
    time.sleep(6)
    screenshot("00-first-start")
    adb("root")
    time.sleep(3)
    adb("wait-for-device")
    version = re.search(r"versionName=(\S+)", sh(f"dumpsys package {PKG}")).group(1)
    accounts = {"accounts": {CUSTOM_ID: {
        "providerId": CUSTOM_ID, "displayName": "Mock STT", "apiKey": "test-key-not-secret",
        "customBaseUrl": "http://10.0.2.2:8080/v1/", "transcriptionModel": "whisper-1"}}}
    seed_prefs({
        "internal__is_ime_set_up": ("b", "true"),
        "internal__notification_permission_state": ("s", encode("GRANTED")),
        "internal__version_on_install": ("s", encode(version)),
        "internal__version_last_use": ("s", encode(version)),
        "internal__version_last_whats_new": ("s", encode(version)),
        "internal__version_last_changelog": ("s", encode(version)),
        "custom__auto_check_updates": ("b", "false"),
        "dictate__provider_accounts": ("s", encode(json.dumps(accounts, separators=(",", ":")))),
        "dictate__provider_accounts_migrated": ("b", "true"),
        "dictate__transcription_provider_id": ("s", encode(CUSTOM_ID)),
        "dictate__push_to_talk": ("b", "false"),
        "dictate__push_to_talk_default_migrated": ("b", "true"),
        "dictate__offline_switch_no_internet_seconds": ("i", "5"),
        # The switch is what is under test, not the silence gate: a quiet CI microphone must not turn
        # scenarios A and C into "no speech detected".
        "dictate__skip_silent_recordings": ("b", "false"),
    })
    open_home()
    screenshot("01-home")

    mic_cache = {}
    steps = [("A", scenario_online), ("E", scenario_hold_backspace), ("C", scenario_no_model), ("install-model", None),
             ("D", scenario_network_error), ("B", scenario_slow_with_model), ("F", scenario_no_internet_with_model),
             ("U", scenario_updater), ("G", scenario_no_network_with_model)]
    for name, fn in steps:
        try:
            if fn is None:
                install_model(args.model_dir)
            else:
                fn(mic_cache, Path(args.wav), stubs)
        except Exception as e:  # keep going: every scenario reports on its own
            check(name, "scenario ran", False, f"{type(e).__name__}: {e}")
            try:
                screenshot(f"{name}-exception")
                logcat_save(f"{name}-exception")
            except Exception:
                pass

    try:
        (OUT / "mock-requests.json").write_text(json.dumps(mock("/requests"), indent=1))
    except Exception as e:
        log(f"mock log unavailable: {e}")
    (OUT / "summary.json").write_text(json.dumps(results, indent=1))
    failed = [r for r in results if not r["ok"]]
    log(f"{len(results) - len(failed)}/{len(results)} checks passed")
    for r in failed:
        log(f"FAILED: [{r['scenario']}] {r['check']} {r['detail']}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write("## Live test\n\n| | Scenario | Check | Detail |\n|---|---|---|---|\n")
            for r in results:
                detail = r["detail"].replace("|", "/")[:160]
                fh.write(f"| {'✅' if r['ok'] else '❌'} | {r['scenario']} | {r['check']} | `{detail}` |\n")
    sys.exit(1 if failed or not results else 0)


if __name__ == "__main__":
    main()
