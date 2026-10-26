"""
PrivaGlot - FastAPI Main Application
"""
import os
from fastapi import FastAPI, WebSocket, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
import shutil
from datetime import datetime
from . import config, vendors
from .verify_key import verify_api_key, probe_llm, probe_tts, probe_asr
from .channels.web_ws import handle_websocket
from .student import student_store
from .teacher import teacher_store
from .course_engine import course_engine, validate_book_dict
from .logging_config import log as logger

app = FastAPI(title="PrivaGlot")

# Mount static files
static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
async def startup_event():
    """Pre-load models at startup"""
    # ffmpeg is the one dependency that is NOT a Python package, so it is the
    # one that silently breaks a fresh install: without it every recording
    # fails at webm->wav and the browser only says "audio conversion failed".
    # Fail loudly here instead, where a human is still watching the console.
    if shutil.which("ffmpeg") is None:
        logger.error("=" * 72)
        logger.error("ffmpeg NOT FOUND on PATH - voice input will not work.")
        logger.error("Windows: run 一键启动.bat again, it installs ffmpeg for you.")
        logger.error("Others : https://www.gyan.dev/ffmpeg/builds/ (put it on PATH)")
        logger.error("=" * 72)
    else:
        logger.info("ffmpeg: found on PATH")

    logger.info("Loading course engine...")
    course_engine.load()
    textbook_names = course_engine.get_textbook_names()
    total_units = sum(len(course_engine.get_textbook(t)["units"]) for t in textbook_names)
    logger.info(f"Course engine ready: {len(textbook_names)} textbook(s), {total_units} unit(s)")
    logger.info("ASR: MiMo cloud (no pre-load needed)")


@app.get("/")
async def root():
    """Serve the main HTML page"""
    html_file = static_dir / "index.html"
    return FileResponse(str(html_file), media_type="text/html")


@app.websocket("/ws/audio")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time audio communication"""
    await handle_websocket(websocket)


@app.get("/health")
async def health():
    """Health check endpoint"""
    try:
        default_voice = teacher_store.default_teacher().voice
    except Exception:
        default_voice = config.DEFAULT_VOICE
    return {
        "status": "ok",
        "version": "4.0",
        "textbooks": course_engine.get_textbook_names(),
        "tts_voice": default_voice,
        "llm_model": config.LLM_MODEL,
    }


_MODALITY_LABELS = {"asr": "语音识别ASR", "llm": "思考模型LLM", "tts": "语音合成TTS"}
_MODALITIES = ("asr", "llm", "tts")


def _modality_catalog(mod: str) -> list:
    """该模态下所有声明了此能力的厂商目录（前端只渲染不持有——未来新增
    厂商 = 档案一份，此表自动多一行，前端零改动）。Key 态随厂商级别下发。"""
    out = []
    seen_key_envs = {}
    for vid, prof in vendors._REGISTRY.items():
        sec = prof.get(mod) or {}
        if not sec:
            continue
        key_env = sec.get("key_env", "")
        key_val = (os.getenv(key_env) or "").strip() if key_env else ""
        if key_env and key_env not in seen_key_envs:
            seen_key_envs[key_env] = bool(key_val)
        out.append({
            "id": vid,
            "display_name": prof.get("display_name", vid),
            "short_name": prof.get("short_name", vid),
            "signup_url": prof.get("signup_url", ""),
            "key_set": bool(key_val),
            "key_masked": config.masked_key(key_val) if key_val else "",
            "models": [{"id": m["id"], "label": m.get("label", m["id"]),
                        "note": m.get("note", "")}
                       for m in sec.get("models", [])],
        })
    return out


def _modality_state(mod: str) -> dict:
    """模态当前态：解析结果＋是否「显式配置」（配置页三列就绪门的关键——
    读法B过渡期的默认回落不算就绪，UI 落地日起无默认值生效）。"""
    sel = getattr(vendors, f"{mod}_selection")()
    explicit = bool((os.getenv(f"{mod.upper()}_VENDOR") or "").strip())
    key_env = (sel.get("section") or {}).get("key_env", "")
    has_key = bool((os.getenv(key_env) or "").strip()) if key_env else False
    models = (sel.get("section") or {}).get("models") or []
    note = next((m.get("note", "") for m in models if m.get("id") == sel["model"]), "")
    return {
        "label": _MODALITY_LABELS[mod],
        "vendor_id": sel["vendor_id"],
        "vendor_name": (sel.get("profile") or {}).get("display_name", ""),
        "model": sel["model"],
        "note": note,
        "explicit": explicit,
        "configured": explicit and has_key,
    }


@app.get("/api/setup/status")
async def setup_status():
    """网页首屏靠这个决定要不要弹「模型配置」页（#15 配置页期）。

    configured = 三模态全部「显式配置」（各列选了厂商且 Key 在位）——
    旧的单 Key 判定随无默认值生效一起退役。vendor 段（默认厂商引导素材）
    保留兼容旧消费方。前端只渲染不持有厂商事实：三列目录由 catalog 下发。
    """
    v = vendors.active()
    modalities = {mod: _modality_state(mod) for mod in _MODALITIES}
    return {
        "configured": all(m["configured"] for m in modalities.values()),
        "modalities": modalities,
        "catalog": {mod: _modality_catalog(mod) for mod in _MODALITIES},
        "base_url": config.LLM_BASE_URL,
        "model": config.LLM_MODEL,
        "referral_url": v["signup_url"],
        "vendor": {**v["guide"], "display_name": v["display_name"],
                   "signup_url": v["signup_url"]},
    }


@app.post("/api/setup/config")
async def setup_config(payload: dict):
    """配置页保存（#15，2026-10-21 用户裁定）：三模态候选配置各强测
    3 次、三次全过才算成功；全部通过才应用+持久化——任一失败旧配置
    原样不动（事务性）。Key 按厂商去重：留空 = 沿用该厂商已保存的 Key。"""
    mods = (payload or {}).get("modalities") or {}
    wanted = {}
    for mod in _MODALITIES:
        entry = mods.get(mod) or {}
        vendor_id = str(entry.get("vendor") or "").strip()
        model = str(entry.get("model") or "").strip()
        key = str(entry.get("key") or "").strip()
        sec = (vendors._REGISTRY.get(vendor_id) or {}).get(mod) or {}
        if not vendor_id or not model or not sec:
            return JSONResponse(
                {"ok": False, "error": f"{_MODALITY_LABELS[mod]} 配置不完整（厂商/模型未选）"},
                status_code=400)
        key_env = sec.get("key_env", "")
        if not key:
            key = (os.getenv(key_env) or "").strip() if key_env else ""
        if not key:
            return JSONResponse(
                {"ok": False, "error": f"{_MODALITY_LABELS[mod]} 还没有填 API Key"},
                status_code=400)
        base = (sec.get("base_url") or "").strip() or config.base_url_for_key(key)
        wanted[mod] = {"vendor": vendor_id, "model": model,
                       "key": key, "base": base}

    probes = {"llm": probe_llm, "tts": probe_tts, "asr": probe_asr}
    results = {}
    all_ok = True
    for mod in _MODALITIES:
        w = wanted[mod]
        attempts = []
        for _ in range(3):
            result = await probes[mod](w["key"], w["base"], w["model"], w["vendor"])
            attempts.append({"code": result.code, "message": result.message})
            if not result.ok:
                break  # 三次全过语义：首败即不可能满足，省掉余下的调用
        ok = len(attempts) == 3 and all(a["code"] == "ok" for a in attempts)
        results[mod] = {"label": _MODALITY_LABELS[mod], "ok": ok,
                        "attempts": attempts}
        all_ok = all_ok and ok

    if not all_ok:
        return {"ok": False, "results": results,
                "message": "配置未保存：有模态未通过探活（三次全过才生效），旧配置没有被改动。"}

    written = config.apply_modality_config(wanted)
    return {"ok": True, "results": results, "written": sorted(written),
            "combo": {mod: _modality_state(mod) for mod in _MODALITIES}}


@app.get("/api/setup/vendor-key")
async def reveal_vendor_key(vendor: str = Query(...)):
    """「显示 Key」：按厂商返回完整 Key。仅本机无鉴权形态下提供——
    与 .env 明文落盘同权（本机就能读文件），不新增暴露面。"""
    try:
        key_env = config.vendor_key_env(vendor)
    except ValueError:
        return JSONResponse({"error": f"未知厂商: {vendor}"}, status_code=404)
    return {"vendor": vendor, "key": (os.getenv(key_env) or "").strip()}


@app.post("/api/setup/vendor-key")
async def save_vendor_key(payload: dict):
    """「保存 Key」（Key 管理与三列保存解耦，#15 配置页期）：保存前用该
    厂商第一个可用模态轻探活一次——坏 Key 不落盘（防好 Key 被误覆盖后
    丢失）。三列的「保存并测活」仍是最终全量门。"""
    vendor = str((payload or {}).get("vendor") or "").strip()
    key = str((payload or {}).get("key") or "").strip()
    sec = None
    for mod in ("llm", "tts", "asr"):
        sec = (vendors._REGISTRY.get(vendor) or {}).get(mod) or {}
        if sec:
            break
    if not vendor or not sec:
        return JSONResponse({"ok": False, "message": f"未知厂商: {vendor}"},
                            status_code=400)
    if not key:
        return JSONResponse({"ok": False, "message": "Key 不能为空"},
                            status_code=400)

    base = (sec.get("base_url") or "").strip() or config.base_url_for_key(key)
    model = ((sec.get("models") or [{}])[0].get("id")) or ""
    if mod == "llm":
        result = await probe_llm(key, base, model, vendor)
    elif mod == "tts":
        result = await probe_tts(key, base, model, vendor)
    else:
        result = await probe_asr(key, base, model, vendor)
    if not result.ok:
        return {"ok": False, "message": result.message,
                "code": result.code}

    env_name = config.set_vendor_key(vendor, key)
    return {"ok": True, "masked": config.masked_key(key), "env": env_name}


@app.post("/api/setup/key")
async def setup_key(payload: dict):
    """网页上提交 Key：先真调一次接口验证，通过了才写进 data/.env。

    这一步是整条引导的关键 —— 用户不必在命令行里粘贴，也不必重启服务：
    验证通过后内存里的配置立刻生效，课马上就能上。
    """
    key = str((payload or {}).get("key") or "").strip().strip('"').strip("'")
    base = config.base_url_for_key(key) if key else config.LLM_BASE_URL
    result = await verify_api_key(key, base, config.LLM_MODEL, config.TTS_MODEL)
    if result.ok:
        config.apply_api_key(key)
    return {
        "ok": result.ok,
        "code": result.code,
        "message": result.message,
        "detail": result.detail,
    }


@app.get("/api/teachers")
async def list_teachers(student_id: str = ""):
    """List teacher profiles plus the given student's remembered pick.

    Read-only on purpose: the frontend picks a teacher, never edits one.
    """
    try:
        default_teacher = teacher_store.default_teacher()
        default_teacher_id = default_teacher.id
    except Exception:
        default_teacher_id = ""

    selected = None
    if student_id:
        teacher, style, _ = teacher_store.resolve_for_student(student_id)
        selected = {"teacher_id": teacher.id, "style": style}

    return {
        "teachers": teacher_store.list_teachers(),
        "styles": [
            {"id": style_id, "label": preset["label"]}
            for style_id, preset in config.STYLE_PRESETS.items()
        ],
        "default_teacher_id": default_teacher_id,
        "selected": selected,
    }


@app.post("/api/teacher-choice")
async def set_teacher_choice(payload: dict):
    """Remember a student's teacher/style pick so the next session starts the
    same way. An explicit teacher change resets the style to that teacher's
    own default."""
    student_id = str(payload.get("student_id") or "").strip()
    teacher_id = str(payload.get("teacher_id") or "").strip()
    if not student_id or not teacher_id:
        return JSONResponse(
            {"error": "student_id and teacher_id are required"}, status_code=400)

    try:
        teacher_store.get_teacher(teacher_id)
    except Exception:
        return JSONResponse(
            {"error": f"Unknown teacher: {teacher_id}"}, status_code=404)

    teacher_store.remember_choice(student_id, teacher_id, payload.get("style"))
    teacher, style, _ = teacher_store.resolve_for_student(student_id)
    return {"selected": {"teacher_id": teacher.id, "style": style}}


@app.get("/api/students")
async def list_students():
    """List all student profiles for the frontend picker."""
    return {"students": student_store.list_students()}


@app.post("/api/students")
async def create_student(payload: dict):
    """Create a new student profile from the frontend picker form."""
    name = str(payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)

    interests = payload.get("interests") or []
    if isinstance(interests, str):
        interests = [i.strip() for i in interests.split(",") if i.strip()]

    try:
        age = int(payload.get("age") or 8)
    except (TypeError, ValueError):
        age = 8

    profile = student_store.create_student(
        name=name,
        age=age,
        language_background=payload.get("language_background") or "中文",
        interests=interests,
    )
    students = student_store.list_students()
    created = next((s for s in students if s["id"] == profile.profile_path.stem), None)
    return {"student": created}


@app.put("/api/students/{student_id}")
async def update_student(student_id: str, payload: dict):
    """Edit the human-settable profile fields (#55). Name is not editable
    (the profile filename is the identity key) and interests are not
    editable (the free-mode teacher's memory). Takes effect for sessions
    started afterwards; unknown id → 404, bad age → 400."""
    try:
        profile = student_store.get_profile(student_id)
        profile.load()
    except (ValueError, FileNotFoundError):
        return JSONResponse({"error": "student not found"}, status_code=404)

    age = payload.get("age")
    if age is not None:
        try:
            age = int(age)
        except (TypeError, ValueError):
            return JSONResponse({"error": "age must be a number"},
                                status_code=400)
    profile.update_profile(
        age=age,
        language_background=payload.get("language_background"),
    )
    students = student_store.list_students()
    updated = next((s for s in students if s["id"] == student_id), None)
    return {"student": updated}


@app.get("/api/curriculum/textbooks")
async def list_textbooks_with_progress(student_id: str = Query(...)):
    """
    List all textbooks and their units with student practice status.
    Books the student practiced most recently come first; the rest by title.
    """
    try:
        profile = student_store.get_profile(student_id)
        profile.load()
    except Exception:
        return JSONResponse({"error": "student not found"}, status_code=404)

    textbooks = []
    textbooks_units = {}

    for textbook_id in course_engine.get_textbook_names():
        tb = course_engine.get_textbook(textbook_id)
        units_summary = course_engine.list_units_summary(textbook_id)
        textbooks_units[textbook_id] = [u["unit_id"] for u in units_summary]

        all_statuses = profile.get_all_textbook_statuses({textbook_id: textbooks_units[textbook_id]})
        for unit in units_summary:
            unit["status"] = all_statuses.get(f"{textbook_id}/{unit['unit_id']}", "not_started")
            # 评估器给的 0-100 进度；从未评估过（或材料已作废）为 null
            unit["progress"] = profile.get_unit_progress(textbook_id, unit["unit_id"]).get("progress")

        textbooks.append({
            "name": textbook_id,
            # name 是身份键（进度记录用它），title 是展示名——选课页只显示 title。
            "title": course_engine.book_title(textbook_id),
            "source": tb.get("source", "user"),
            "units": units_summary,
            # 最近一次练到这本书的时间，只用于排序，不返回给前端
            "last_practiced": _book_last_practiced(profile, textbook_id,
                                                   textbooks_units[textbook_id]),
        })

    # 最近练过的书排最前，其余按标题；排序键是标量，负时间戳即倒序。
    textbooks.sort(key=lambda t: (_neg_last_practiced(t.pop("last_practiced")),
                                  t["title"].lower(), t["name"]))
    return {"textbooks": textbooks}


def _book_last_practiced(profile, textbook_id: str, unit_ids: list[str]) -> str:
    """这本书在该学生进度里的最近练习时间（isoformat），没练过返回空串。"""
    stamps = []
    progress = profile.course_progress
    for unit_id in unit_ids:
        entry = progress.get(f"{textbook_id}/{unit_id}") or {}
        stamp = str(entry.get("last_practiced") or "").strip()
        if stamp:
            stamps.append(stamp)
    return max(stamps) if stamps else ""


def _neg_last_practiced(stamp: str) -> float:
    """升序排序键：练得越晚键越小（排最前），从没练过排最后。"""
    try:
        return -datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError):
        return float("inf")


@app.post("/api/curriculum/reload")
async def reload_curriculum():
    """重扫教材目录。保存新教材后前端会调它（save 内部也会重扫，
    这个接口留给手工放文件进目录后不想重启服务的场景）。"""
    course_engine.load()
    names = course_engine.get_textbook_names()
    total_units = sum(len(course_engine.get_textbook(t)["units"]) for t in names)
    return {"ok": True, "textbooks": len(names), "units": total_units}


@app.post("/api/curriculum/validate")
async def validate_curriculum(payload: dict):
    """校验「我的教材」里组装出的教材对象，把问题用人话说回来。

    前端在「检查」和「保存」前各调一次；规则与保存时完全同一套
    （validate_book_dict），不会出现检查通过却保存失败。
    """
    units = (payload or {}).get("units") or []
    problems = validate_book_dict({
        "title": (payload or {}).get("title"),
        "units": units,
    })
    return {"ok": not problems, "problems": problems}


@app.post("/api/curriculum/books")
async def save_curriculum_book(payload: dict):
    """保存「我的教材」创建的新教材：校验 → 生成 id/unit_id → 写入
    data/curriculum/ → 重扫 catalog。id 冲突靠自动加序号解决，
    永不覆盖已有教材。"""
    title = str((payload or {}).get("title") or "").strip()
    units = (payload or {}).get("units") or []
    problems = validate_book_dict({"title": title, "units": units})
    if problems:
        return JSONResponse({"ok": False, "problems": problems}, status_code=400)

    result = course_engine.save_new_book(title, units)
    names = course_engine.get_textbook_names()
    return {
        "ok": True,
        "id": result["id"],
        "title": result["title"],
        "textbooks": len(names),
        "units": sum(len(course_engine.get_textbook(t)["units"]) for t in names),
    }


@app.get("/api/curriculum/books/{book_id}")
async def get_curriculum_book(book_id: str):
    """读回一本自建教材的完整内容（编辑器预填用）。捆绑示例不可编辑，返回 404。"""
    book = course_engine.get_user_book(book_id)
    if book is None:
        return JSONResponse(
            {"error": "教材不存在，或它不是可编辑的自建教材"}, status_code=404)
    return book


@app.put("/api/curriculum/books/{book_id}")
async def update_curriculum_book(book_id: str, payload: dict):
    """保存编辑：校验 → 原文件覆盖写回（id 与已有 unit_id 不变）→ 重扫。"""
    title = str((payload or {}).get("title") or "").strip()
    units = (payload or {}).get("units") or []
    problems = validate_book_dict({"title": title, "units": units})
    if problems:
        return JSONResponse({"ok": False, "problems": problems}, status_code=400)

    result = course_engine.update_book(book_id, title, units)
    if result is None:
        return JSONResponse(
            {"ok": False, "problems": ["教材不存在，或它不是可编辑的自建教材"]},
            status_code=404)
    return {"ok": True, "id": result["id"], "title": result["title"]}


@app.delete("/api/curriculum/books/{book_id}")
async def delete_curriculum_book(book_id: str):
    """删除一本自建教材。学生进度保留（休眠键）。捆绑示例拒绝删除。"""
    if not course_engine.delete_book(book_id):
        return JSONResponse(
            {"ok": False, "problems": ["教材不存在，或它不是可删除的自建教材"]},
            status_code=404)
    names = course_engine.get_textbook_names()
    return {"ok": True, "textbooks": len(names)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT)
