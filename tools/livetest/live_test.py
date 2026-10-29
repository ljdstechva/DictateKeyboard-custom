#!/usr/bin/env python3
"""Live emulator test for the custom build's offline switch and transcribing animation.

Runs on a GitHub-hosted runner next to a booted emulator (see .github/workflows/live-test.yml):

  * the APK is installed, set as the keyboard and pointed at a mock OpenAI-compatible server on the
    runner (tools/livetest/mock_stt_server.py, reached from the emulator as 10.0.2.2);
  * speech is a TTS WAV streamed into the emulator microphone over gRPC (tools/inject_emulator_audio.py);
  * dictation is started and stopped by tapping the keyboard's mic, exactly as a user would.

Scenarios (the mock decides how slow "online" is):

  A  online answers after 3 s                -> online text is typed, no offline switch
  C  online hangs, no offline model          -> request cancelled at ~10 s, "download an offline model" error
  D  online connection breaks, model present -> offline model takes over at once (network error path)
  B  online hangs, offline model present     -> request cancelled at ~10 s, offline model types the text

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
# Pixel 6 / API 34 positions from upstream's own latency benchmark (tools/benchmark_dictation_latency.ps1).
FALLBACK_MIC = (1015, 1625)

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
    log(f"keyboard enabled and selected: {ime_id}")


def open_home():
    sh(f"am force-stop {PKG}")
    sh(f"am start -W -n {ACTIVITY}")
    time.sleep(4)


def focus_field(tag):
    xml = dump_ui(f"{tag}-home")
    n = field_node(xml)
    if n is None:
        screenshot(f"{tag}-no-field")
        raise RuntimeError("try-out field not found on the home screen")
    tap(center(n))
    if not wait_for(keyboard_shown, 10):
        raise RuntimeError("keyboard did not open on the try-out field")
    time.sleep(2.5)


def find_mic(tag):
    """The Dictate mic in the keyboard: an IME node whose label reads like it, else upstream's spot."""
    xml = dump_ui(f"{tag}-keyboard")
    ime = [n for n in nodes(xml) if n.get("package") == PKG and "EditText" not in n.get("class", "")]
    lines = [f"{n.get('class')} desc={n.get('content-desc')!r} text={n.get('text')!r} {n.get('bounds')}"
             for n in ime]
    (OUT / f"{tag}-ime-nodes.txt").write_text("\n".join(lines) + "\n")
    pattern = re.compile(r"dictat|record|microphone|\bmic\b|voice|speak", re.I)
    for n in ime:
        label = f"{n.get('content-desc', '')} {n.get('text', '')}"
        if pattern.search(label) and "stop" not in label.lower():
            xy = center(n)
            log(f"mic found by label {label.strip()!r} at {xy}")
            return xy
    log(f"mic not labelled in the dump ({len(ime)} keyboard nodes), using {FALLBACK_MIC}")
    return FALLBACK_MIC


def record(mic, wav, stubs, tag):
    """Tap mic, stream the WAV into the emulator microphone, tap mic again. Returns the stop time."""
    tap(mic)
    time.sleep(1.0)
    screenshot(f"{tag}-recording")
    sent = OUT / f"{tag}.sent"
    done = OUT / f"{tag}.done"
    proc = subprocess.Popen([sys.executable, str(ROOT / "tools/inject_emulator_audio.py"), str(wav),
                             "--stubs", str(stubs), "--packet-ms", "300",
                             "--sent-marker", str(sent), "--completed-marker", str(done)])
    if not wait_for(sent.exists, 40, 0.1):
        proc.kill()
        raise RuntimeError("audio injection did not finish streaming")
    tap(mic)
    stopped = time.monotonic()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    log(f"recording stopped; injection completed={done.exists()}")
    for marker in (sent, done):
        marker.unlink(missing_ok=True)
    return stopped


def check(scenario, name, ok, detail=""):
    results.append({"scenario": scenario, "check": name, "ok": bool(ok), "detail": detail})
    log(f"{'PASS' if ok else 'FAIL'} [{scenario}] {name} {detail}")


def offline_switch_line(text):
    m = re.search(r"phase=offlineSwitch reason=(\S+) sinceTranscribingMs=(\d+) model=(\S+) used=(\S+)", text)
    return m.groups() if m else None


def last_request():
    reqs = mock("/requests")
    return reqs[-1] if reqs else {}


# --- scenarios ------------------------------------------------------------------------------------

def scenario_online(mic_cache, wav, stubs):
    tag = "A-online"
    mock("/control?delay=3&mode=normal")
    open_home()
    focus_field(tag)
    mic = mic_cache.setdefault("mic", find_mic(tag))
    screenshot(f"{tag}-keyboard")
    logcat_clear()
    stopped = record(mic, wav, stubs, tag)
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


def scenario_no_model(mic_cache, wav, stubs):
    tag = "C-slow-no-model"
    mock("/control?delay=60&mode=normal")
    open_home()
    focus_field(tag)
    mic = mic_cache.get("mic") or find_mic(tag)
    logcat_clear()
    stopped = record(mic, wav, stubs, tag)
    time.sleep(max(0, 4.0 - (time.monotonic() - stopped)))
    screenshot(f"{tag}-transcribing-4s")
    line = wait_for(lambda: offline_switch_line(latency_lines()), 20, 0.5)
    time.sleep(1.5)
    screenshot(f"{tag}-error")
    time.sleep(2)
    lat = latency_lines()
    logcat_save(tag)
    text = field_text()
    check(tag, "offline switch fired on the deadline", line and line[0] == "deadline", repr(line))
    if line:
        ms = int(line[1])
        check(tag, "switch at ~10 s of transcribing", 9000 <= ms <= 12500, f"{ms} ms")
        check(tag, "no model reported", line[2] == "none" and line[3] == "false", repr(line))
    check(tag, "error shown (apiError TIMEOUT)", re.search(r"phase=apiError kind=TIMEOUT", lat), "")
    check(tag, "field stays empty", not text, repr(text))
    req = last_request()
    check(tag, "online request cancelled by the app", req.get("outcome") == "client-closed" and
          req.get("afterSeconds", 99) <= 12.5, json.dumps(req))


def scenario_network_error(mic_cache, wav, stubs):
    tag = "D-network-error-model"
    mock("/control?delay=0&mode=reset")
    open_home()
    focus_field(tag)
    mic = mic_cache.get("mic") or find_mic(tag)
    logcat_clear()
    record(mic, wav, stubs, tag)
    line = wait_for(lambda: offline_switch_line(latency_lines()), 30, 0.5)
    text = wait_for(lambda: field_text(), 90, 2.0)
    screenshot(f"{tag}-result")
    logcat_save(tag)
    # Three retries 3 s apart come close to the 10 s budget, so either trigger is a correct handover.
    check(tag, "offline model used after a network error",
          line and line[0] in ("NETWORK", "deadline") and line[3] == "true", repr(line))
    if line:
        check(tag, "handover within ~10 s", int(line[1]) <= 12500, f"{line[1]} ms")
    check(tag, "offline text typed", text and ONLINE_TEXT not in text, repr(text))
    mock("/control?mode=normal")


def scenario_slow_with_model(mic_cache, wav, stubs):
    tag = "B-slow-with-model"
    mock("/control?delay=60&mode=normal")
    open_home()
    focus_field(tag)
    mic = mic_cache.get("mic") or find_mic(tag)
    logcat_clear()
    stopped = record(mic, wav, stubs, tag)
    time.sleep(max(0, 4.0 - (time.monotonic() - stopped)))
    screenshot(f"{tag}-online-4s")
    line = wait_for(lambda: offline_switch_line(latency_lines()), 20, 0.3)
    time.sleep(0.8)
    screenshot(f"{tag}-offline")
    text = wait_for(lambda: field_text(), 120, 2.0)
    screenshot(f"{tag}-result")
    logcat_save(tag)
    check(tag, "offline switch fired on the deadline", line and line[0] == "deadline", repr(line))
    if line:
        ms = int(line[1])
        check(tag, "switch at ~10 s of transcribing", 9000 <= ms <= 12500, f"{ms} ms")
        check(tag, "downloaded model used", line[2] == MODEL_ID and line[3] == "true", repr(line))
    check(tag, "offline text typed (not the online text)", text and ONLINE_TEXT not in text, repr(text))
    req = last_request()
    check(tag, "online request cancelled by the app", req.get("outcome") == "client-closed" and
          req.get("afterSeconds", 99) <= 12.5, json.dumps(req))


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
    args = parser.parse_args()
    OUT = Path(args.out)
    OUT.mkdir(parents=True, exist_ok=True)
    stubs = OUT.parent / "grpc-stubs"
    make_stubs(stubs)

    log("device: " + sh("getprop ro.build.version.release").strip() + " / " + sh("wm size").strip())
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
        "dictate__offline_switch_seconds": ("i", "10"),
    })
    open_home()
    screenshot("01-home")

    mic_cache = {}
    steps = [("A", scenario_online), ("C", scenario_no_model), ("install-model", None),
             ("D", scenario_network_error), ("B", scenario_slow_with_model)]
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
