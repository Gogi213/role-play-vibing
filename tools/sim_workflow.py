"""Стенд имитации воркфлоу Role Play Vibing (Рентген-симулятор).
Эмулирует сквозной цикл жизни задачи:
1. Запись решения владельца в реестр (registry.py decide).
2. Постановка тикета CEO (tickets.py new).
3. Хук контекста роли (role_context.py SessionStart).
4. Нагрузка и отказы охранника команд (delete_guard.py PreToolUse).
5. Хук контроля памяти и инъекций хода (role_memory.py UserPromptSubmit).
6. Работа роли (Инженер) и сдача результата (tickets.py result done).
7. Тик диспетчера (dispatch.py --once) с mock claude CLI.
8. Сдача работы Судье и цикл возврата / приёмки (tickets.py result accept).
9. Сторож (watch.py --once), Надзиратель (overseer.py --dry-run), Доктор (doctor.py).
Замеряет задержки, объемы контекста, накладные расходы и узкие места.
"""
import os
import sys
import time
import json
import shutil
import tempfile
import subprocess
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

def run_cmd(args, env=None, cwd=None):
    e = os.environ.copy()
    if env:
        e.update(env)
    p = subprocess.run(args, cwd=cwd, env=e, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr

def run_simulation():
    tmp_dir = Path(tempfile.mkdtemp(prefix="rpv_xray_sim_"))
    results = {}
    try:
        print(f"=== [РЕНТГЕН-СТЕНД] Инициализация окружения: {tmp_dir} ===")
        # Создаем git-репозиторий проекта
        subprocess.run(["git", "init"], cwd=tmp_dir, capture_output=True)
        subprocess.run(["git", "config", "user.email", "owner@sim.local"], cwd=tmp_dir, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Owner"], cwd=tmp_dir, capture_output=True)
        (tmp_dir / "README.md").write_text("# Test Simulated Project\n")
        subprocess.run(["git", "add", "."], cwd=tmp_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_dir, capture_output=True)

        # Разворачиваем структуру .claude/roles как делает rpv-init
        roles_dir = tmp_dir / ".claude" / "roles"
        roles_dir.mkdir(parents=True)
        (roles_dir / "notes").mkdir(parents=True)
        for f in (PLUGIN_ROOT / "templates" / "roles").glob("*.md"):
            shutil.copy(f, roles_dir / f.name)
        (tmp_dir / ".claude" / "tickets").mkdir(parents=True)
        (tmp_dir / ".claude" / "goals").mkdir(parents=True)
        (tmp_dir / ".claude" / "errors").mkdir(parents=True)
        (tmp_dir / ".claude" / "decisions.md").write_text("# Решения владельца\n")

        # Создаем mock claude binary в bin директории
        mock_bin_dir = tmp_dir / "bin"
        mock_bin_dir.mkdir()
        mock_claude = mock_bin_dir / "claude"
        mock_claude.write_text("#!/bin/sh\necho 'Mock Claude executed'\n")
        mock_claude.chmod(0o755)

        base_env = {
            "RPV_PROJECT": str(tmp_dir),
            "CLAUDE_PROJECT_DIR": str(tmp_dir),
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PATH": f"{mock_bin_dir}:{os.environ.get('PATH', '')}",
            "PYTHONPATH": f"{PLUGIN_ROOT}:{PLUGIN_ROOT / '.claude' / 'dispatcher'}",
        }

        # 1. registry decide (решение владельца)
        t0 = time.perf_counter()
        rc, out, err = run_cmd([
            sys.executable, str(PLUGIN_ROOT / ".claude" / "dispatcher" / "registry.py"),
            "--project", str(tmp_dir), "decide",
            "--quote", "Нужно сделать оптимизацию модуля с замером скорости",
            "--gist", "Оптимизация модуля"
        ], env=base_env, cwd=tmp_dir)
        results["t_decide_ms"] = (time.perf_counter() - t0) * 1000
        print(f"1. registry decide: {results['t_decide_ms']:.1f} ms (rc={rc})")

        # 2. tickets new
        t0 = time.perf_counter()
        rc, out, err = run_cmd([
            sys.executable, str(PLUGIN_ROOT / ".claude" / "dispatcher" / "tickets.py"),
            "--project", str(tmp_dir), "new",
            "--owner", "engineer",
            "--title", "Оптимизировать модуль",
            "--ref", "В-1",
            "--desc", "Договор: [тест: python -m unittest] тесты зеленые, замер скорости"
        ], env=base_env, cwd=tmp_dir)
        results["t_new_ms"] = (time.perf_counter() - t0) * 1000
        tid = "TK-001"
        print(f"2. tickets new: {results['t_new_ms']:.1f} ms (rc={rc}, tid={tid})")

        # 3. Хук SessionStart (role_context.py)
        e_role = base_env.copy()
        e_role["RPV_ROLE"] = "engineer"
        e_role["RPV_TICKET"] = tid
        session_start_payload = json.dumps({
            "session_id": "sim-eng-sess-001",
            "transcript_path": "",
            "hook_event_name": "SessionStart",
            "source": "startup"
        })
        t0 = time.perf_counter()
        p = subprocess.run([sys.executable, str(PLUGIN_ROOT / ".claude" / "hooks" / "role_context.py")],
                           input=session_start_payload.encode("utf-8"), capture_output=True, cwd=tmp_dir, env=e_role)
        results["t_hook_session_start_ms"] = (time.perf_counter() - t0) * 1000
        d_out = json.loads(p.stdout.decode("utf-8")) if p.stdout else {}
        ctx = d_out.get("hookSpecificOutput", {}).get("additionalContext", "")
        results["session_start_injected_chars"] = len(ctx)
        print(f"3. role_context (SessionStart): {results['t_hook_session_start_ms']:.1f} ms, инжектировано: {results['session_start_injected_chars']} символов")

        # 4. Охранник команд delete_guard (PreToolUse) - замер нормальной и запрещенной команды
        guard_hook = [sys.executable, str(PLUGIN_ROOT / ".claude" / "hooks" / "delete_guard.py")]
        tool_payload_allow = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "echo 'result data' > out.txt"}
        })
        t0 = time.perf_counter()
        p_allow = subprocess.run(guard_hook, input=tool_payload_allow.encode("utf-8"), capture_output=True, cwd=tmp_dir, env=e_role)
        results["t_guard_allow_ms"] = (time.perf_counter() - t0) * 1000

        tool_payload_deny = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin main"}
        })
        t0 = time.perf_counter()
        p_deny = subprocess.run(guard_hook, input=tool_payload_deny.encode("utf-8"), capture_output=True, cwd=tmp_dir, env=e_role)
        results["t_guard_deny_ms"] = (time.perf_counter() - t0) * 1000
        print(f"4. delete_guard: allow {results['t_guard_allow_ms']:.1f} ms (rc={p_allow.returncode}), deny {results['t_guard_deny_ms']:.1f} ms (rc={p_deny.returncode})")

        # 5. Хук role_memory (UserPromptSubmit) - замер инъекций в каждый ход
        prompt_payload = json.dumps({
            "session_id": "sim-eng-sess-001",
            "transcript_path": "",
            "prompt": "Как лучше сделать оптимизацию?",
            "hook_event_name": "UserPromptSubmit"
        })
        t0 = time.perf_counter()
        p_mem = subprocess.run([sys.executable, str(PLUGIN_ROOT / ".claude" / "hooks" / "role_memory.py")],
                               input=prompt_payload.encode("utf-8"), capture_output=True, cwd=tmp_dir, env=e_role)
        results["t_hook_user_prompt_ms"] = (time.perf_counter() - t0) * 1000
        d_mem = json.loads(p_mem.stdout.decode("utf-8")) if p_mem.stdout else {}
        ctx_mem = d_mem.get("hookSpecificOutput", {}).get("additionalContext", "")
        results["user_prompt_injected_chars"] = len(ctx_mem)
        print(f"5. role_memory (UserPromptSubmit): {results['t_hook_user_prompt_ms']:.1f} ms, инжектировано: {results['user_prompt_injected_chars']} символов")

        # 6. Исполнитель сдает работу (result done)
        (tmp_dir / "out.txt").write_text("Optimized algorithm: 10x faster\n")
        (tmp_dir / "test_run.py").write_text("import unittest\nclass T(unittest.TestCase):\n  def test_opt(self): self.assertTrue(True)\n")
        subprocess.run(["git", "add", "."], cwd=tmp_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add optimization"], cwd=tmp_dir, capture_output=True)

        t0 = time.perf_counter()
        rc, out, err = run_cmd([
            sys.executable, str(PLUGIN_ROOT / ".claude" / "dispatcher" / "tickets.py"),
            "--project", str(tmp_dir), "result",
            tid, "done",
            "--why", "Оптимизация завершена, алгоритм ускорен",
            "--path", "out.txt"
        ], env=e_role, cwd=tmp_dir)
        results["t_result_done_ms"] = (time.perf_counter() - t0) * 1000
        print(f"6. tickets result done: {results['t_result_done_ms']:.1f} ms (rc={rc}): {out.strip()}")

        # 7. Диспетчер обрабатывает done и передает Судье
        t0 = time.perf_counter()
        rc, out, err = run_cmd([
            sys.executable, str(PLUGIN_ROOT / ".claude" / "dispatcher" / "dispatch.py"),
            "--project", str(tmp_dir), "--once"
        ], env=base_env, cwd=tmp_dir)
        results["t_dispatch_handoff_ms"] = (time.perf_counter() - t0) * 1000
        print(f"7. dispatch --once (эстафета Судье): {results['t_dispatch_handoff_ms']:.1f} ms (rc={rc}): {out.strip()}")

        # 8. Судья принимает работу (result accept)
        e_judge = base_env.copy()
        e_judge["RPV_ROLE"] = "judge"
        e_judge["RPV_TICKET"] = tid
        t0 = time.perf_counter()
        rc, out, err = run_cmd([
            sys.executable, str(PLUGIN_ROOT / ".claude" / "dispatcher" / "tickets.py"),
            "--project", str(tmp_dir), "result",
            tid, "accept",
            "--why", "Все проверено и соответствует договору",
            "--decisions", "В-1",
            "--numbers", "-",
            "--path", "out.txt",
            "--tail", "-"
        ], env=e_judge, cwd=tmp_dir)
        results["t_result_accept_ms"] = (time.perf_counter() - t0) * 1000
        print(f"8. judge accept: {results['t_result_accept_ms']:.1f} ms (rc={rc}): {out.strip()} {err.strip()}")

        # 9. Повторный тик диспетчера (закрытие тикета и сигнал CEO)
        t0 = time.perf_counter()
        rc, out, err = run_cmd([
            sys.executable, str(PLUGIN_ROOT / ".claude" / "dispatcher" / "dispatch.py"),
            "--project", str(tmp_dir), "--once"
        ], env=base_env, cwd=tmp_dir)
        results["t_dispatch_close_ms"] = (time.perf_counter() - t0) * 1000
        print(f"9. dispatch --once (закрытие и нотификация): {results['t_dispatch_close_ms']:.1f} ms (rc={rc}): {out.strip()}")

        # 10. Нагрузка фоновых надсмотрщиков: watch.py, overseer.py, doctor.py
        t0 = time.perf_counter()
        rc, out, err = run_cmd([sys.executable, str(PLUGIN_ROOT / ".claude" / "dispatcher" / "watch.py"), "--project", str(tmp_dir), "--once"], env=base_env, cwd=tmp_dir)
        results["t_watch_once_ms"] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        rc, out, err = run_cmd([sys.executable, str(PLUGIN_ROOT / ".claude" / "dispatcher" / "overseer.py"), "--project", str(tmp_dir), "--dry-run"], env=base_env, cwd=tmp_dir)
        results["t_overseer_dry_ms"] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        rc, out, err = run_cmd([sys.executable, str(PLUGIN_ROOT / ".claude" / "dispatcher" / "doctor.py"), "--project", str(tmp_dir)], env=base_env, cwd=tmp_dir)
        results["t_doctor_ms"] = (time.perf_counter() - t0) * 1000
        print(f"10. Фоны: watch={results['t_watch_once_ms']:.1f} ms, overseer={results['t_overseer_dry_ms']:.1f} ms, doctor={results['t_doctor_ms']:.1f} ms")

        # Сохраним метрики симуляции
        (PLUGIN_ROOT / "docs" / "bench" / "sim_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print("=== Симуляция успешно завершена, результаты сохранены в docs/bench/sim_results.json ===")
        return results
    finally:
        shutil.rmtree(tmp_dir)

if __name__ == "__main__":
    run_simulation()
