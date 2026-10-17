"""
PrivaGlot - FastAPI Main Application
"""
from fastapi import FastAPI, WebSocket, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
import shutil
from datetime import datetime
from . import config, vendors
from .verify_key import verify_api_key
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


@app.get("/api/setup/status")
async def setup_status():
    """网页首屏靠这个决定要不要弹「先填 API Key」的引导卡片。

    引导素材（标题/步骤/注册链接/充值提示）由供应商档案下发
    （app/vendors/<厂商>.py），前端只渲染不持有文案 —— 改厂商活动
    或换厂商只动档案，前端零改动。
    """
    v = vendors.active()
    return {
        "configured": config.is_configured(),
        "base_url": config.LLM_BASE_URL,
        "model": config.LLM_MODEL,
        "referral_url": v["signup_url"],
        "vendor": {**v["guide"], "display_name": v["display_name"],
                   "signup_url": v["signup_url"]},
    }


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
        teacher, style = teacher_store.resolve_for_student(student_id)
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
    teacher, style = teacher_store.resolve_for_student(student_id)
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
