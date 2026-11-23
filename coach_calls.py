"""
== 운영 맥락 ==
역할: 폰 통화 녹음(Syncthing 으로 PC 녹음 폴더에 넘어옴)을 받아써서, 코치가 수강생·업체 질문에 답한 내용을
      「질문 → 답」 지식으로 쌓는다. 챗봇이 이 지식을 읽고 다른 수강생 질문에 코치 방식으로 답하게 하는 게 목적(자산화).
입력: settings.txt 의 녹음폴더(하위 폴더까지) 안 소리 파일. .env 의 ANTHROPIC_API_KEY.
      받아쓰기: ffmpeg(16kHz 변환) + whisper-cli(whisper.cpp) + ggml 모델 파일. PC 안에서만 돈다.
      정리: Claude API(settings.txt 모델). 받아쓴 글만 보낸다(소리는 안 보냄).
출력: 지식/질문답.json(정본) → 지식/<주제>.md, 지식/전체.md(챗봇에 넣을 파일).
      처리기록.json(한 번 본 파일은 다시 안 함), 기록.log.
      받아쓴 원문은 남기지 않는다. 녹음은 처리 뒤 지운다(녹음지우기=예, Syncthing 받기 전용이라 폰 원본은 그대로).
흐름: 새 파일 → 다 넘어왔는지 확인(크기 변화 없음) → 제외 상대면 버림 → 받아쓰기 → 질문·답 뽑기(개인정보 지움)
      → 기존 지식과 겹치면 합치고 아니면 새로 → md 다시 씀.
의도적 미구현: 챗봇에 자동 올리기(챗봇 서비스가 정해지면 붙인다), 화자 구분, 받아쓴 원문 보관.
마지막 점검: 2026-11-23 (맥에서 시험 녹음으로 끝까지. 윈도우는 코치 PC 첫 통화로 확인)
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HOME = Path(__file__).resolve().parent
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".amr", ".3gp", ".aac", ".ogg", ".opus", ".flac"}
STATE_FILE = HOME / "처리기록.json"
LOG_FILE = HOME / "기록.log"
KB_DIR = HOME / "지식"
KB_FILE = KB_DIR / "질문답.json"
STABLE_SEC = 60          # 이 시간 동안 크기가 안 바뀌어야 다 넘어온 것으로 본다
LOOP_SEC = 60            # 폴더를 다시 보는 간격
MIN_CHARS = 80           # 받아쓴 글이 이보다 짧으면 정리하지 않는다(부재중·끊긴 통화)


# ---------------------------------------------------------------- 설정
def load_kv(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def load_config() -> dict:
    c = load_kv(HOME / "settings.txt")
    env = load_kv(HOME / ".env")
    key = os.environ.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY", "")
    home = Path.home()
    def p(v, default):
        return Path(os.path.expanduser(v or default))
    return {
        "inbox": p(c.get("녹음폴더"), str(home / "코치-녹음")),
        "whisper": c.get("whisper") or "whisper-cli",
        "ffmpeg": c.get("ffmpeg") or "ffmpeg",
        "model_file": p(c.get("받아쓰기모델"), str(home / ".whisper" / "ggml-large-v3-turbo.bin")),
        "ai_model": c.get("AI모델") or "claude-opus-5-5",
        "start": c.get("시작일") or "",
        "delete": (c.get("녹음지우기") or "예").strip() in ("예", "yes", "y", "1", "true"),
        "exclude": [x.strip() for x in (c.get("제외") or "").split(",") if x.strip()],
        "coach": c.get("코치이름") or "코치",
        "field": c.get("분야") or "",
        "api_key": key,
    }


# ---------------------------------------------------------------- 기록
def log(msg: str):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 파일 찾기
def file_key(f: Path) -> str:
    st = f.stat()
    return f"{f.name}|{st.st_size}|{int(st.st_mtime)}"


def call_time(f: Path) -> Optional[dt.datetime]:
    """갤럭시 녹음 파일 이름 끝 YYMMDD_HHMMSS"""
    m = re.search(r"(\d{6})_(\d{6})", f.stem)
    if m:
        try:
            return dt.datetime.strptime(m.group(1) + m.group(2), "%y%m%d%H%M%S")
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(f.stat().st_mtime)


def peer_of(f: Path) -> str:
    """파일 이름에서 통화 상대(폰 연락처 이름 또는 번호). '통화 녹음 홍길동_251123_101010' → 홍길동"""
    s = re.sub(r"_?\d{6}_\d{6}.*$", "", f.stem)
    s = re.sub(r"^(통화\s*녹음|Call\s*recording)\s*", "", s, flags=re.I)
    return s.strip(" _-")


def find_new(cfg, state) -> List[Path]:
    inbox = cfg["inbox"]
    if not inbox.exists():
        log(f"녹음폴더가 없습니다: {inbox}")
        return []
    now = time.time()
    start = None
    if cfg["start"]:
        try:
            start = dt.datetime.strptime(cfg["start"], "%Y-%m-%d")
        except ValueError:
            log(f"시작일 형식이 틀렸습니다(예: 2026-11-23): {cfg['start']}")
    out = []
    for f in inbox.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXT:
            continue
        if any(part.startswith(".") or part.startswith("~syncthing") for part in f.relative_to(inbox).parts):
            continue
        try:
            if now - f.stat().st_mtime < STABLE_SEC:
                continue  # 아직 넘어오는 중일 수 있다
            k = file_key(f)
        except FileNotFoundError:
            continue
        if k in state["seen"]:
            continue
        if start and (call_time(f) or dt.datetime.now()) < start:
            state["seen"][k] = {"result": "시작일 전", "at": dt.datetime.now().isoformat(timespec="seconds")}
            continue
        out.append(f)
    return sorted(out, key=lambda x: x.stat().st_mtime)


# ---------------------------------------------------------------- 받아쓰기
def transcribe(cfg, f: Path) -> str:
    if not cfg["model_file"].exists():
        raise RuntimeError(f"받아쓰기 모델 파일이 없습니다: {cfg['model_file']}")
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        r = subprocess.run([cfg["ffmpeg"], "-y", "-loglevel", "error", "-i", str(f), "-ar", "16000", "-ac", "1", str(wav)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not wav.exists():
            raise RuntimeError("소리 파일 변환 실패(ffmpeg): " + (r.stderr or "").strip()[-200:])
        out = Path(td) / "o"
        r = subprocess.run([cfg["whisper"], "-m", str(cfg["model_file"]), "-l", "ko", "-f", str(wav),
                            "-otxt", "-of", str(out), "-np"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        txt = out.with_suffix(".txt")
        if r.returncode != 0 or not txt.exists():
            raise RuntimeError("받아쓰기 실패(whisper-cli): " + (r.stderr or "").strip()[-200:])
        return txt.read_text(encoding="utf-8", errors="replace").strip()


# ---------------------------------------------------------------- 정리(Claude)
class QA(BaseModel):
    topic: str          # 짧은 주제 이름(파일 이름이 됨)
    question: str       # 다른 수강생도 물을 법한 일반 질문
    answer: str         # 코치 답 요지(왜 그런지, 예시 포함)
    keywords: List[str]


class Extract(BaseModel):
    useful: bool        # 질문·답이 하나라도 있나
    reason: str         # 없으면 왜(잡담, 일정 잡기, 사적 통화 등)
    items: List[QA]


class MergeAction(BaseModel):
    new_index: int              # 새 항목 번호(0부터)
    merge_into: Optional[str]   # 합칠 기존 항목 id, 새로 만들면 null
    question: str               # 합친 뒤 질문(새로 만들면 그대로)
    answer: str                 # 합친 뒤 답


class MergePlan(BaseModel):
    actions: List[MergeAction]


def client(cfg):
    import anthropic
    if not cfg["api_key"]:
        raise RuntimeError(".env 에 ANTHROPIC_API_KEY 가 없습니다")
    return anthropic.Anthropic(api_key=cfg["api_key"])


def ask(cfg, system: str, user: str, schema):
    r = client(cfg).messages.parse(
        model=cfg["ai_model"],
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=schema,
    )
    if r.stop_reason == "refusal":
        raise RuntimeError("AI가 이 통화 정리를 거절했습니다")
    if r.parsed_output is None:
        raise RuntimeError(f"AI 답을 읽지 못했습니다(stop_reason={r.stop_reason})")
    return r.parsed_output


def extract(cfg, transcript: str, topics: List[str]) -> Extract:
    field = f" 분야는 {cfg['field']}이다." if cfg["field"] else ""
    system = (
        f"너는 {cfg['coach']}의 통화 녹음을 받아쓴 글에서, 수강생이나 업체가 물은 것과 {cfg['coach']}가 답한 것을 뽑아 "
        f"챗봇 지식으로 만드는 정리 담당이다.{field}\n"
        "규칙:\n"
        "- 질문 하나에 답 하나로 나눈다. 다른 수강생도 물을 법한 일반 질문으로 바꿔 쓴다.\n"
        "- 답은 코치가 실제로 한 말의 요지만 쓴다. 코치가 안 한 말을 지어내지 않는다. 이유와 예시를 코치가 말했으면 넣는다.\n"
        "- 사람 이름, 업체명, 상호, 전화번호, 주소, 계좌, 매출·금액처럼 그 사람만의 정보는 모두 지운다. "
        "필요하면 '한 음식점', '월 매출이 적은 초기 업체'처럼 일반화한다.\n"
        "- 잡담, 일정 잡기, 결제·환불 처리, 사적 통화, 답이 없는 통화는 useful=false.\n"
        "- 받아쓰기라 오타가 있다. 뜻이 분명할 때만 고쳐 읽고, 알아들을 수 없는 부분은 버린다.\n"
        "- topic 은 2~8자 한국어 명사구. 아래 기존 주제 중 맞는 게 있으면 그 이름을 그대로 쓴다.\n"
        "- 모든 글은 한국어 존댓말 평서문."
    )
    user = "기존 주제: " + (", ".join(topics) if topics else "(없음)") + "\n\n받아쓴 글:\n" + transcript
    return ask(cfg, system, user, Extract)


def merge(cfg, kb, new_items: List[QA]) -> MergePlan:
    if not kb["items"]:
        return MergePlan(actions=[MergeAction(new_index=i, merge_into=None, question=q.question, answer=q.answer)
                                  for i, q in enumerate(new_items)])
    index = "\n".join(f"[{x['id']}] ({x['topic']}) {x['question']}\n  답: {x['answer'][:300]}" for x in kb["items"])
    news = "\n".join(f"<{i}> ({q.topic}) {q.question}\n  답: {q.answer}" for i, q in enumerate(new_items))
    system = (
        "너는 챗봇 지식(질문→답 모음)을 관리한다. 새 항목마다 기존 항목과 같은 질문인지 판단한다.\n"
        "- 뜻이 같은 질문이면 merge_into 에 기존 id 를 넣고, 기존 답과 새 답을 합쳐 하나의 답으로 다시 쓴다. "
        "겹치는 말은 한 번만, 새로 더해진 내용은 살린다. 둘이 서로 다르게 말하면 둘 다 남기고 어떤 경우에 어느 쪽인지 적는다.\n"
        "- 비슷해 보여도 다른 질문이면 merge_into 는 null, 질문과 답은 새 항목 그대로.\n"
        "- 모든 새 항목에 대해 actions 를 하나씩 낸다."
    )
    user = f"기존 항목:\n{index}\n\n새 항목:\n{news}"
    return ask(cfg, system, user, MergePlan)


# ---------------------------------------------------------------- 지식 저장
def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", s).strip(" .")
    return s[:40] or "기타"


def apply(kb, new_items: List[QA], plan: MergePlan, when: str) -> tuple:
    by_id = {x["id"]: x for x in kb["items"]}
    added = merged = 0
    for a in plan.actions:
        if a.new_index < 0 or a.new_index >= len(new_items):
            continue
        q = new_items[a.new_index]
        tgt = by_id.get(a.merge_into) if a.merge_into else None
        if tgt:
            tgt["question"], tgt["answer"] = a.question, a.answer
            tgt["keywords"] = sorted(set(tgt.get("keywords", [])) | set(q.keywords))
            tgt["count"] = tgt.get("count", 1) + 1
            tgt["updated"] = when
            merged += 1
        else:
            kb["seq"] = kb.get("seq", 0) + 1
            item = {"id": f"Q{kb['seq']:04d}", "topic": q.topic.strip(), "question": a.question, "answer": a.answer,
                    "keywords": q.keywords, "count": 1, "created": when, "updated": when}
            kb["items"].append(item)
            by_id[item["id"]] = item
            added += 1
    return added, merged


def render(kb, cfg):
    KB_DIR.mkdir(parents=True, exist_ok=True)
    groups = {}
    for x in kb["items"]:
        groups.setdefault(x["topic"] or "기타", []).append(x)
    wanted = set()
    def block(x):
        kw = f"\n\n키워드: {', '.join(x['keywords'])}" if x.get("keywords") else ""
        return f"### {x['question']}\n\n{x['answer']}{kw}\n"
    for topic, items in sorted(groups.items()):
        name = safe_name(topic) + ".md"
        wanted.add(name)
        body = f"# {topic}\n\n{cfg['coach']} 통화에서 나온 질문과 답입니다.\n\n" + "\n".join(block(x) for x in items)
        (KB_DIR / name).write_text(body, encoding="utf-8")
    allmd = f"# {cfg['coach']} 질문·답 모음\n\n" + "\n".join(
        f"## {t}\n\n" + "\n".join(block(x) for x in items) for t, items in sorted(groups.items()))
    (KB_DIR / "전체.md").write_text(allmd, encoding="utf-8")
    wanted.add("전체.md")
    for f in KB_DIR.glob("*.md"):   # 주제 이름이 바뀌어 남은 옛 파일 정리(이 프로그램이 만든 것만 있는 폴더)
        if f.name not in wanted:
            f.unlink()


# ---------------------------------------------------------------- 한 건 처리
def process(cfg, state, kb, f: Path):
    k = file_key(f)
    when = (call_time(f) or dt.datetime.now()).isoformat(timespec="minutes")
    peer = peer_of(f)
    rec = {"at": dt.datetime.now().isoformat(timespec="seconds"), "called": when}
    if any(x and x in peer for x in cfg["exclude"]):
        rec["result"] = "제외 상대"
        log(f"건너뜀(제외 상대): {f.name}")
    else:
        t0 = time.time()
        text = transcribe(cfg, f)
        log(f"받아쓰기 {len(text)}자 ({time.time() - t0:.0f}초): {f.name}")
        if len(text) < MIN_CHARS:
            rec["result"] = "너무 짧음"
            log(f"너무 짧아 넘김: {f.name}")
        else:
            ex = extract(cfg, text, sorted({x["topic"] for x in kb["items"]}))
            if not ex.useful or not ex.items:
                rec["result"] = "지식 없음: " + ex.reason[:80]
                log(f"지식 없음({ex.reason[:60]}): {f.name}")
            else:
                plan = merge(cfg, kb, ex.items)
                added, merged = apply(kb, ex.items, plan, when)
                write_json(KB_FILE, kb)
                render(kb, cfg)
                rec["result"] = f"새로 {added}개, 합침 {merged}개"
                log(f"지식 새로 {added}개 · 합침 {merged}개: {f.name}")
    state["seen"][k] = rec
    write_json(STATE_FILE, state)
    if cfg["delete"]:
        try:
            f.unlink()
        except Exception as e:
            log(f"녹음 지우기 실패({e}): {f.name}")


def run_once(cfg) -> int:
    state = read_json(STATE_FILE, {"seen": {}})
    state.setdefault("seen", {})
    if not state.get("started"):
        state["started"] = dt.datetime.now().isoformat(timespec="seconds")
        if not cfg["start"]:
            cfg["start"] = dt.date.today().isoformat()
            log(f"처음 켰습니다. 오늘({cfg['start']}) 이후 통화만 정리합니다. 바꾸려면 settings.txt 시작일")
        state["start"] = cfg["start"]
    if not cfg["start"]:
        cfg["start"] = state.get("start", "")
    kb = read_json(KB_FILE, {"seq": 0, "items": []})
    files = find_new(cfg, state)
    write_json(STATE_FILE, state)
    n = 0
    for f in files:
        try:
            process(cfg, state, kb, f)
            n += 1
        except Exception as e:
            log(f"실패: {f.name} · {e}")
            fails = state.setdefault("fail", {})
            k = file_key(f)
            fails[k] = fails.get(k, 0) + 1
            if fails[k] >= 3:    # 세 번 실패한 파일은 넘긴다(녹음은 남겨 둠)
                state["seen"][k] = {"result": "실패 3번: " + str(e)[:120], "at": dt.datetime.now().isoformat(timespec="seconds")}
                log(f"세 번 실패해서 넘깁니다(녹음은 남김): {f.name}")
            write_json(STATE_FILE, state)
    return n


def check(cfg) -> bool:
    ok = True
    def line(good, msg):
        nonlocal ok
        ok = ok and good
        print(("[됨] " if good else "[안 됨] ") + msg)
    line(cfg["inbox"].exists(), f"녹음폴더 {cfg['inbox']}")
    line(shutil.which(cfg["ffmpeg"]) is not None, f"ffmpeg ({cfg['ffmpeg']})")
    line(shutil.which(cfg["whisper"]) is not None, f"whisper-cli ({cfg['whisper']})")
    line(cfg["model_file"].exists(), f"받아쓰기 모델 {cfg['model_file']}")
    line(bool(cfg["api_key"]), "API 키(.env)")
    try:
        import anthropic  # noqa
        line(True, "anthropic 패키지")
    except ImportError:
        line(False, "anthropic 패키지 (pip install anthropic)")
    return ok


def main():
    ap = argparse.ArgumentParser(description="통화 녹음 → 챗봇 질문·답 지식")
    ap.add_argument("--once", action="store_true", help="지금 있는 것만 한 번 처리하고 끝")
    ap.add_argument("--check", action="store_true", help="설치가 다 됐는지 확인")
    args = ap.parse_args()
    cfg = load_config()
    if args.check:
        sys.exit(0 if check(cfg) else 1)
    if args.once:
        log(f"처리 {run_once(cfg)}건")
        return
    log(f"시작. 녹음폴더 {cfg['inbox']} 를 {LOOP_SEC}초마다 봅니다. 끄려면 이 창을 닫으세요.")
    while True:
        try:
            run_once(cfg)
        except Exception as e:
            log(f"오류: {e}")
        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    main()
