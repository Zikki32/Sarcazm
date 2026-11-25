"""Локальный веб-интерфейс: дашборд отчёта и мини-API.

Запуск:
    python webapp.py                # http://127.0.0.1:8000
    python webapp.py --port 8080 --no-browser

Зависимостей нет: только стандартная библиотека. Данные читаются из отчёта,
который формирует main.py; кнопки в интерфейсе запускают прогон тем же
процессом, что и CLI.
"""

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import threading
import webbrowser
from collections import Counter, deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config import AppConfig, load_config
from market import MARKET_SOURCE, MARKET_UPDATED

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
STATIC_FILES = {"index.html", "styles.css", "app.js"}

MAX_LOG_LINES = 200
MAX_LIMIT = 100


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _mtime_iso(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def _build_summary(
    report: list[dict[str, Any]],
    cfg: AppConfig,
) -> dict[str, Any]:
    grades = Counter(item.get("grade") for item in report if item.get("grade"))
    topics: Counter[str] = Counter()
    types: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    for item in report:
        for issue in item.get("issues") or []:
            topics[issue.get("topic", "unknown")] += 1
            types[issue.get("issue_type", "unknown")] += 1
            sources[issue.get("detected_by", "ai")] += 1

    scores = [item["score"] for item in report if isinstance(item.get("score"), int)]
    good_count = sum(
        1
        for item in report
        if item.get("analysis_error") is None
        and len(item.get("issues") or []) <= cfg.good_max_issues
    )

    schedule_scores: list[int] = []
    salary_scores: list[int] = []
    market_below = market_at = market_above = 0
    schedule_types: Counter[str] = Counter()
    for item in report:
        components = {
            component.get("key"): component
            for component in item.get("score_components") or []
        }
        schedule_component = components.get("schedule")
        if isinstance(schedule_component, dict) and isinstance(schedule_component.get("score"), int):
            schedule_scores.append(schedule_component["score"])
        salary_component = components.get("salary")
        if isinstance(salary_component, dict) and isinstance(salary_component.get("score"), int):
            salary_scores.append(salary_component["score"])

        salary_info = item.get("salary_analysis") or {}
        ratio = salary_info.get("ratio")
        if isinstance(ratio, (int, float)):
            if ratio < 0.85:
                market_below += 1
            elif ratio <= 1.15:
                market_at += 1
            else:
                market_above += 1

        schedule_info = item.get("schedule_analysis") or {}
        type_label = schedule_info.get("type_label")
        if type_label:
            schedule_types[type_label] += 1

    return {
        "generated_at": _mtime_iso(cfg.report_output_path),
        "report_path": str(cfg.report_output_path),
        "good_path": str(cfg.good_vacancies_path),
        "total": len(report),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "avg_schedule_score": (
            round(sum(schedule_scores) / len(schedule_scores), 1) if schedule_scores else 0
        ),
        "avg_salary_score": (
            round(sum(salary_scores) / len(salary_scores), 1) if salary_scores else 0
        ),
        "issues_total": sum(len(item.get("issues") or []) for item in report),
        "good_max_issues": cfg.good_max_issues,
        "good_count": good_count,
        "analysis_errors": sum(1 for item in report if item.get("analysis_error")),
        "market_below": market_below,
        "market_at": market_at,
        "market_above": market_above,
        "market_compared": market_below + market_at + market_above,
        "market": {
            "region": cfg.market_region,
            "average_salary": cfg.market_avg_salary,
            "median_salary": cfg.market_median_salary,
            "source": MARKET_SOURCE,
            "updated": MARKET_UPDATED,
        },
        "by_grade": {grade: grades.get(grade, 0) for grade in ("A", "B", "C", "D")},
        "by_topic": dict(topics),
        "by_type": dict(types),
        "by_source": dict(sources),
        "by_schedule_type": dict(schedule_types),
    }


class RunManager:
    """Один фоновый прогон main.py с журналом для интерфейса."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._log: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self._mode: str | None = None
        self._limit: int | None = None
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._exit_code: int | None = None

    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def start(self, mode: str, limit: int) -> tuple[bool, str]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return False, "Прогон уже выполняется"
            self._log.clear()
            self._mode = mode
            self._limit = limit
            self._started_at = _now_iso()
            self._finished_at = None
            self._exit_code = None

            command = [
                sys.executable,
                str(self.base_dir / "main.py"),
                "--mode",
                mode,
                "--limit",
                str(limit),
            ]
            try:
                # Дочерний процесс пишет в pipe, а значит использует кодировку
                # локали (cp1251/cp866). Принудительно просим UTF-8, иначе
                # русский текст в журнале превращается в "�".
                env = dict(os.environ)
                env["PYTHONIOENCODING"] = "utf-8"
                env["PYTHONUTF8"] = "1"
                self._process = subprocess.Popen(
                    command,
                    cwd=str(self.base_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )
            except OSError as exc:
                self._process = None
                self._finished_at = _now_iso()
                self._exit_code = -1
                self._log.append(f"Не удалось запустить процесс: {exc}")
                return False, f"Не удалось запустить процесс: {exc}"

        thread = threading.Thread(target=self._consume, daemon=True)
        thread.start()
        return True, "Прогон запущен"

    def _consume(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self._log.append(line.rstrip())

        exit_code = process.wait()
        with self._lock:
            self._exit_code = exit_code
            self._finished_at = _now_iso()

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            return {
                "running": running,
                "mode": self._mode,
                "limit": self._limit,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "exit_code": self._exit_code,
                "log": "\n".join(self._log),
            }


class DashboardHandler(BaseHTTPRequestHandler):
    cfg: AppConfig
    run_manager: RunManager
    server_version = "TransparentVacancy/1.0"

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # тише в консоли
        sys.stderr.write("[web] " + fmt % args + "\n")

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, status: int) -> None:
        self._send_json({"error": message}, status=status)

    def _send_static(self, name: str) -> None:
        if name not in STATIC_FILES:
            self._send_error_json("Файл не найден", HTTPStatus.NOT_FOUND)
            return

        path = WEB_DIR / name
        if not path.exists():
            self._send_error_json("Файл не найден", HTTPStatus.NOT_FOUND)
            return

        body = path.read_bytes()
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # Маршруты
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in {"/", "/index.html"}:
                self._send_static("index.html")
            elif path.startswith("/web/"):
                self._send_static(path.removeprefix("/web/"))
            elif path == "/api/report":
                self._send_json(_read_json(self.cfg.report_output_path) or [])
            elif path == "/api/good":
                self._send_json(_read_json(self.cfg.good_vacancies_path) or [])
            elif path == "/api/summary":
                report = _read_json(self.cfg.report_output_path) or []
                summary = _build_summary(report, self.cfg)
                summary["run"] = self.run_manager.status()
                self._send_json(summary)
            elif path == "/api/status":
                self._send_json(self.run_manager.status())
            elif path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
            else:
                self._send_error_json("Не найдено", HTTPStatus.NOT_FOUND)
        except Exception as exc:  # последняя линия защиты для UI
            self._send_error_json(f"Внутренняя ошибка: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/run":
            self._send_error_json("Не найдено", HTTPStatus.NOT_FOUND)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send_error_json("Некорректный JSON в теле запроса", HTTPStatus.BAD_REQUEST)
            return

        mode = str(payload.get("mode", "check")).lower()
        if mode not in {"check", "live"}:
            self._send_error_json("mode должен быть 'check' или 'live'", HTTPStatus.BAD_REQUEST)
            return

        try:
            limit = int(payload.get("limit", 10))
        except (TypeError, ValueError):
            self._send_error_json("limit должен быть числом", HTTPStatus.BAD_REQUEST)
            return
        limit = max(1, min(MAX_LIMIT, limit))

        started, message = self.run_manager.start(mode, limit)
        status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
        self._send_json({"started": started, "message": message}, status=status)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Веб-дашборд «Прозрачная вакансия»")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес прослушивания (по умолчанию 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Порт (по умолчанию 8000)")
    parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер автоматически")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config()

    handler = type(
        "ConfiguredDashboardHandler",
        (DashboardHandler,),
        {"cfg": cfg, "run_manager": RunManager(BASE_DIR)},
    )

    try:
        server = ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        print(f"[ERROR] Не удалось занять {args.host}:{args.port}: {exc}")
        return 1

    url = f"http://{args.host}:{args.port}"
    print(f"[INFO] Дашборд: {url}")
    print(f"[INFO] Отчёт: {cfg.report_output_path}")
    print("[INFO] Ctrl+C — остановить сервер")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] Сервер остановлен")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
