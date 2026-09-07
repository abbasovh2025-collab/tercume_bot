"""
state.json üçün konflikt-həll skripti.
İki run eyni vaxta yaxın state.json-u dəyişəndə git rebase konflikt verir.
Əvvəllər sadəcə "bizim versiyanı saxla" edilirdi — bu, digər run-un qeydlərini
silib atırdı, nəticədə artıq göndərilmiş mesaj/albom "göndərilməmiş" kimi
görünüb TƏKRAR göndərilirdi.

Bu skript hər iki versiyanı DÜZGÜN BİRLƏŞDİRİR:
- last_id: hər kanal üçün ikisindən BÖYÜK olanı götürülür
- msgs / groups / retries: hər iki tərəfin qeydləri BİRLƏŞDİRİLİR (heç biri itmir)
"""
import json
import subprocess


def load_json_from_git(rev: str) -> dict:
    try:
        out = subprocess.run(
            ["git", "show", f"{rev}:state.json"],
            capture_output=True, text=True, check=True
        )
        return json.loads(out.stdout) if out.stdout.strip() else {}
    except Exception:
        return {}


def merge_channel(a: dict, b: dict) -> dict:
    last_ids = [x.get("last_id") for x in (a, b) if x.get("last_id") is not None]
    last_id = max(last_ids) if last_ids else None

    msgs = {}
    msgs.update(a.get("msgs", {}))
    msgs.update(b.get("msgs", {}))

    groups = {}
    groups.update(a.get("groups", {}))
    groups.update(b.get("groups", {}))

    retries = {}
    for k, v in a.get("retries", {}).items():
        retries[k] = max(retries.get(k, 0), v)
    for k, v in b.get("retries", {}).items():
        retries[k] = max(retries.get(k, 0), v)

    return {"last_id": last_id, "msgs": msgs, "groups": groups, "retries": retries}


def merge_state(a: dict, b: dict) -> dict:
    result = {}
    for key in set(a.keys()) | set(b.keys()):
        result[key] = merge_channel(a.get(key, {}), b.get(key, {}))
    return result


if __name__ == "__main__":
    # Rebase konflikti zamanı: :2: = "ours" (origin/main, artıq push olunmuş),
    # :3: = "theirs" (bizim bu run-da replay olunan local commit)
    ours = load_json_from_git(":2:state.json")
    theirs = load_json_from_git(":3:state.json")

    if not ours and not theirs:
        print("⚠️ Heç bir versiya oxuna bilmədi, state.json toxunulmadı.")
    else:
        merged = merge_state(ours, theirs)
        with open("state.json", "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False)
        total_msgs = sum(len(c.get("msgs", {})) for c in merged.values())
        print(f"✅ state.json merge edildi ({len(merged)} kanal, {total_msgs} mesaj qeydi). Heç nə itmədi.")
