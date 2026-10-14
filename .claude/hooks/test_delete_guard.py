"""Проверки `delete_guard.check` (запуск: `python -m unittest .claude/hooks/test_delete_guard.py -v` из корня проекта).

Правило: отказ — только когда КОМАНДНОЕ слово простой команды удаляет и цель вне своей папки (или не определяется);
слова rm/unlink/rmtree в тексте аргументов, комментариях, heredoc-данных, grep-шаблонах — не удаление.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# проверки написаны на примере условного проекта proj: корень задаём окружением ДО импорта, удалённые каталоги — как после
# чтения его `.claude/rpv.json` (умолчаний нет); защищённых путей у этого проекта нет — их проверяют тесты ProtectedPaths,
# подставляя шаблоны на время теста
os.environ["CLAUDE_PROJECT_DIR"] = r"C:\visual projects\proj"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import delete_guard as dg  # noqa: E402

PROJ_RPV = {"guard_remote_roots": ["~/proj/", "$home/proj/", "${home}/proj/", "/home/srv/proj/", "/opt/proj-compute/"],
            "guard_stage": "/dev/shm/proj-stage", "guard_forbidden_hosts": ["203.0.113.1"]}
dg.REMOTE_ROOTS, dg.STAGE, dg.CLOSED_RE, _ = dg.machine_rules(PROJ_RPV)

CWD = r"C:\visual projects\proj"
SCRATCH = "C:/Users/3D6B~1/AppData/Local/Temp/claude/C--visual-projects-proj/abc/scratchpad"


def denied(cmd, cwd=CWD):
    return dg.check(cmd, cwd) is not None


def protect(*patterns, error=None):
    """Защищённые пути проекта на время теста (как после чтения `.claude/rpv.json`): шаблоны приводятся к виду стража."""
    from unittest import mock
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(mock.patch.object(dg, "PROTECTED", tuple(dg.norm(x).rstrip("/") for x in patterns)))
    stack.enter_context(mock.patch.object(dg, "PROTECTED_ERROR", error))
    return stack


def outside_dir(prefix="dg-outside-"):
    """Свежий каталог ВНЕ корней стража и вне временного каталога (временные файлы стражу разрешены): под домашним."""
    d = tempfile.mkdtemp(prefix=prefix, dir=str(Path.home()))
    assert dg.temp_rest(dg.norm(Path(d).as_posix())) is None, d
    return d


class Allowed(unittest.TestCase):
    """Не удаление либо удаление внутри своей папки — пропускается."""

    def ok(self, cmd, cwd=CWD):
        reason = dg.check(cmd, cwd)
        self.assertIsNone(reason, f"ложный отказ: {cmd!r} → {reason}")

    # --- из задачи владельца (02.10): слова в тексте — не удаление
    def test_python_stdin_variable_rd(self):
        self.ok("python - <<'EOF'\nrd = 1\nprint(rd)\nEOF")

    def test_grep_pattern(self):
        self.ok(r"grep 'rmtree\|unlink' file.py")

    def test_ticket_comment_text(self):
        self.ok('python .claude/dispatcher/tickets.py comment TK-1 --author ceo --text "после rm файла"')

    def test_git_commit_message(self):
        self.ok('git commit -m "unlink готов"')

    def test_cat_heredoc_data(self):
        self.ok("cat > notes.md <<'EOF'\nunlink-скрипт готов\nEOF")

    # --- удаление внутри своей папки
    def test_rm_in_project(self):
        self.ok('rm -rf "/c/visual projects/proj/data/tmp"')

    def test_rm_in_project_windows_path(self):
        self.ok('rm -rf "C:\\visual projects\\proj\\data\\tmp"')

    def test_rm_relative_in_project(self):
        self.ok("rm -f data/tmp/x.csv")

    def test_rm_in_scratchpad(self):
        self.ok(f'rm -rf "{SCRATCH}/sim"')

    def test_ssh_srv_proj_subdir(self):
        self.ok("ssh srv@192.0.2.49 'rm -rf ~/proj/tk026/stage'")

    def test_ssh_srv_ram_stage(self):
        self.ok("ssh srv@192.0.2.49 'rm -rf /dev/shm/proj-stage'")

    def test_ssh_srv_ram_stage_inner(self):
        self.ok("ssh srv@192.0.2.49 'rm -rf /dev/shm/proj-stage/2026-01-03/run'")

    def test_ssh_srv_ram_stage_glob(self):
        self.ok("ssh -i ~/.ssh/id_rsa -o BatchMode=yes srv@192.0.2.49 'rm -rf /dev/shm/proj-stage/*'")

    def test_ssh_vps_compute_subdir(self):
        self.ok('ssh ubuntu@203.0.113.2 "rm -rf /opt/proj-compute/tk025/tmp"')

    def test_ssh_cd_then_relative(self):
        self.ok("ssh srv@192.0.2.49 'cd ~/proj/tk026 && rm -rf stage'")

    def test_systemd_run_wrapper(self):
        self.ok("ssh srv@192.0.2.49 \"systemd-run --user --unit=x bash -c 'rm -rf ~/proj/tk026/stage'\"")

    # --- слова удаления не в позиции команды
    def test_echo_rm(self):
        self.ok('echo "rm -rf /c/Windows"')

    def test_printf_rmtree(self):
        self.ok("printf 'shutil.rmtree(\"/x\")\\n' > snippet.txt")

    def test_rg_unlink(self):
        self.ok("rg -n 'os.remove|unlink\\(' src tools")

    def test_git_log_grep(self):
        self.ok('git log --grep="rm -rf" --oneline')

    def test_commit_message_heredoc_in_substitution(self):
        self.ok("git commit -m \"$(cat <<'EOF'\nУдалил rm -rf /x; don't unlink (никогда)\nEOF\n)\"")

    def test_comment_with_apostrophe(self):
        self.ok("ls data # don't rm anything")

    def test_python_script_file(self):
        self.ok("python tools/compute/tk025-recompute.py --selftest")

    def test_python_c_no_delete(self):
        self.ok('python -c "import os; print(os.listdir(\'.\'))"')

    def test_python_list_remove_is_not_os_remove(self):
        self.ok("python -c \"xs=[1,2]; xs.remove(1); print(xs)\"")

    def test_python_heredoc_variable_rmtree_word(self):
        self.ok("python - <<'EOF'\nrmtree_count = 0\nunlink_note = 'unlink'\nprint(rmtree_count, unlink_note)\nEOF")

    def test_find_without_delete(self):
        self.ok("find /c/Windows -name '*.log' -print")

    def test_find_exec_grep(self):
        self.ok("find . -name '*.py' -exec grep -l rmtree {} +")

    def test_git_status_and_clean_dry_run(self):
        self.ok("git status --short && git clean -nd")

    def test_rsync_without_delete(self):
        self.ok("rsync -a /c/Windows/x srv@192.0.2.49:~/proj/y/")

    def test_cd_then_rm_in_project_subdir(self):
        self.ok('cd "/c/visual projects/proj/data" && rm -rf tmp')

    def test_find_delete_in_project(self):
        self.ok("find data/tmp -name '*.part' -delete")

    def test_find_exec_rm_in_project(self):
        self.ok("find data/tmp -name '*.part' -exec rm -f {} \\;")

    def test_python_rmtree_in_project(self):
        self.ok("python -c \"import shutil; shutil.rmtree('/c/visual projects/proj/data/tmp')\"")

    def test_python_pathlib_in_project(self):
        self.ok("python - <<'EOF'\nfrom pathlib import Path\nPath('data/tmp/x.csv').unlink()\nEOF")

    def test_rm_variable_known_literal(self):
        self.ok('T="/c/visual projects/proj/data/tmp"; rm -rf "$T"')

    def test_ssh_options_array(self):
        self.ok("K=(-i /c/Users/x/.ssh/id_rsa -o BatchMode=yes); H=srv@192.0.2.49\n"
                "ssh \"${K[@]}\" $H 'cd ~/proj; rm -f tmp-p07/t9.finished; ls queue'")

    def test_ssh_command_variable_with_options(self):
        self.ok("S=\"ssh -o BatchMode=yes -i /c/Users/x/.ssh/id_rsa srv@192.0.2.49\"; $S 'rm -rf ~/proj/tk026/stage'")

    def test_local_rm_with_ssh_elsewhere_in_command(self):
        self.ok('cd "/c/visual projects/proj" && rm "tools/compute/old.sh" && S="ssh -i k srv@192.0.2.49"; $S ls')

    def test_git_rm_tracked_file(self):
        self.ok("git rm --cached tools/old.sh")

    def test_docker_rm_container(self):
        self.ok("docker rm -f proj-test")

    def test_robocopy_copy_only(self):
        self.ok(r"robocopy data\a data\b /E")

    def test_perl_without_delete(self):
        self.ok("perl -e 'print 1'")

    def test_ssh_unknown_options_array_before_host(self):
        self.ok("ssh \"${KEY[@]}\" srv@192.0.2.49 'rm -f ~/proj/tmp-t38/bench14.DONE'")

    def test_rsync_remove_source_files_destination_is_not_checked(self):
        self.ok("rsync -a --remove-source-files -e 'ssh -i k' /opt/proj-compute/x/ backup@backup.example:proj/x/")

    def test_powershell_assignment_of_here_string(self):
        self.ok("$script = @'\nrm -rf /tmp/x\n'@")

    def test_wrapper_script_with_host_arg_inside_policy(self):
        self.ok("/tmp/sshx srv@192.0.2.49 'cd ~/proj && rm -f tmp-p07/read-a.log && ls'")

    def test_wrapper_script_closed_host_without_deletion(self):
        self.ok("/tmp/sshx ubuntu@203.0.113.1 'ls /opt/proj/data | head'")

    def test_shell_function_wrapper_inside_policy(self):
        self.ok("rsh() { ssh -i k srv@192.0.2.49 \"$@\"; }\nrsh 'rm -rf ~/proj/tk026/stage'")

    def test_echo_with_email_and_rm_text(self):
        self.ok("echo srv@192.0.2.49 'rm foo'")

    def test_script_creation_heredoc_over_ssh_is_data(self):
        self.ok("ssh srv@192.0.2.49 'cat > ~/proj/x.sh <<\"EOF\"\nrm -rf /home/srv/other\nEOF\nchmod +x ~/proj/x.sh'")

    def test_powershell_script_string_to_srv_inside_policy(self):
        self.ok("$script = @'\nrm -f ~/proj/tmp-p07/x.log\n'@\nssh -i k srv@192.0.2.49 $script")

    def test_help_flag(self):
        self.ok("rm --help")

    def test_remove_item_whatif(self):
        self.ok("Remove-Item C:\\Windows\\x -Recurse -WhatIf")

    def test_remove_item_in_project(self):
        self.ok('Remove-Item -LiteralPath "C:\\visual projects\\proj\\data\\tmp" -Recurse -Force')

    def test_two_commands_both_fine(self):
        self.ok("ls data && rm -f data/tmp/a.txt data/tmp/b.txt")


class Denied(unittest.TestCase):
    """Настоящее удаление вне своей папки или с непроверяемой целью — отказ."""

    def no(self, cmd, cwd=CWD):
        self.assertIsNotNone(dg.check(cmd, cwd), f"пропущено: {cmd!r}")

    # --- из задачи владельца
    def test_rm_users(self):
        self.no("rm -rf /c/Users/x")

    def test_ssh_closed_host(self):
        self.no("ssh ubuntu@203.0.113.1 'rm /opt/proj/data/x.bin'")

    def test_find_root_delete(self):
        self.no("find / -name x -delete")

    def test_python_rmtree_windows(self):
        self.no("python -c \"import shutil; shutil.rmtree('/c/Windows/x')\"")

    def test_remove_item_windows_single_backslash(self):
        self.no("Remove-Item C:\\Windows\\x -Recurse")

    def test_remove_item_windows_double_backslash(self):
        self.no("Remove-Item C:\\\\Windows\\\\x -Recurse")

    # --- закрытые хосты (guard_forbidden_hosts)
    def test_closed_host_overrides_remote_roots(self):
        self.no("ssh ubuntu@203.0.113.1 'rm -rf /opt/proj-compute/x'")
        self.no("ssh ubuntu@203.0.113.1 'rm -rf ~/proj/x'")

    def test_closed_host_via_variable(self):
        self.no('SSHC="ssh -i ~/.ssh/id_rsa"; $SSHC ubuntu@203.0.113.1 "rm -f ~/proj/x"')

    def test_closed_host_heredoc_shell(self):
        self.no("ssh ubuntu@203.0.113.1 <<'EOF'\nrm -rf ~/proj/epochs\nEOF")

    def test_closed_host_by_identity_file_name(self):
        self.no("ssh -i ~/.ssh/id_203.0.113.1 ubuntu@alias 'rm -f ~/proj/x'")

    def test_rsync_delete_to_closed_host(self):
        self.no("rsync -a --delete data/x/ ubuntu@203.0.113.1:~/proj/x/")

    # --- обёртки и вложенность
    def test_sudo_rm(self):
        self.no("sudo rm -rf /var/lib/x")

    def test_env_assignment_rm(self):
        self.no("FOO=1 rm -rf /etc/x")

    def test_nohup_xargs_rm(self):
        self.no("ls /tmp | nohup xargs rm -rf")

    def test_xargs_rm_even_with_project_dir(self):
        self.no("ls data/tmp | xargs rm -f")

    def test_bash_c(self):
        self.no("bash -c 'rm -rf /c/Users/x'")

    def test_sh_lc(self):
        self.no('sh -lc "rm -rf /home/other/x"')

    def test_ssh_bash_c_nested(self):
        self.no("ssh srv@192.0.2.49 \"bash -c 'rm -rf /home/srv/other'\"")

    def test_powershell_command(self):
        self.no('powershell -NoProfile -Command "Remove-Item C:\\Windows\\x -Recurse"')

    def test_cmd_c_rd(self):
        self.no('cmd /c "rd /s /q C:\\Windows\\x"')

    def test_cmd_del(self):
        self.no("del /f /q C:\\Windows\\x")

    def test_command_substitution(self):
        self.no('echo "$(rm -rf /c/Users/x)"')

    def test_backtick_substitution(self):
        self.no("echo `rm -rf /c/Users/x`")

    def test_chain_after_innocent(self):
        self.no('git commit -m "ok" && rm -rf /c/Users/x')

    def test_pipe_into_shell(self):
        self.no("echo 'rm -rf /c/Users/x' | bash")

    def test_heredoc_into_sh(self):
        self.no("sh <<'EOF'\nrm -rf /c/Users/x\nEOF")

    def test_eval_string(self):
        self.no('eval "rm -rf /c/Users/x"')

    def test_stdin_python_heredoc_rmtree(self):
        self.no("python - <<'EOF'\nimport shutil\nshutil.rmtree('/c/Users/x')\nEOF")

    def test_python_os_remove(self):
        self.no("python3 -c \"import os; os.remove('/etc/hosts')\"")

    def test_python_alias_import(self):
        self.no("python - <<'EOF'\nfrom shutil import rmtree as rt\nrt('/c/Users/x')\nEOF")

    def test_python_pathlib_unlink(self):
        self.no("python - <<'EOF'\nfrom pathlib import Path\nPath('/c/Users/x/y.txt').unlink()\nEOF")

    def test_python_variable_target(self):
        self.no("python - <<'EOF'\nimport os\nfor f in os.listdir('.'):\n    os.remove(f)\nEOF")

    def test_python_const_propagation_outside(self):
        self.no("python - <<'EOF'\nimport shutil\np = '/c/Users/' + 'x'\nshutil.rmtree(p)\nEOF")

    def test_python_subprocess_rm(self):
        self.no("python -c \"import subprocess; subprocess.run(['rm', '-rf', '/c/Users/x'])\"")

    def test_python_os_system_rm(self):
        self.no("python -c \"import os; os.system('rm -rf /c/Users/x')\"")

    def test_ssh_python_heredoc(self):
        self.no("ssh srv@192.0.2.49 python3 - <<'EOF'\nimport shutil\nshutil.rmtree('/home/srv/other')\nEOF")

    def test_python_syntax_error_fallback(self):
        self.no("python -c \"import shutil; shutil.rmtree(\"")

    # --- непроверяемые цели
    def test_rm_variable(self):
        self.no('rm -rf "$DIR"')

    def test_rm_variable_prefix_unknown(self):
        self.no('rm -rf $HOME_X/data')

    def test_rm_without_target(self):
        self.no("rm -rf")

    def test_rm_glob_root_of_project(self):
        self.no('rm -rf "/c/visual projects/proj/*"')

    def test_rm_project_root(self):
        self.no('rm -rf "/c/visual projects/proj"')

    def test_rm_dot(self):
        self.no("rm -rf .")

    def test_rm_parent(self):
        self.no("rm -rf ../x")

    def test_rm_relative_after_cd_out(self):
        self.no("cd /c/Users/x && rm -rf data")

    def test_rm_relative_in_ssh(self):
        self.no("ssh srv@192.0.2.49 'rm -rf tk026/stage'")

    def test_rm_substitution_target(self):
        self.no("rm -rf $(cat list.txt)")

    def test_pipeline_remove_item(self):
        self.no("Get-ChildItem C:\\x | Remove-Item -Recurse")

    def test_remove_item_parenthesized(self):
        self.no("Remove-Item (Join-Path $env:TEMP 'x') -Recurse")

    def test_ssh_unknown_host_variable(self):
        self.no("ssh $BOX 'rm -f ~/proj/x'")

    def test_find_exec_rm_outside(self):
        self.no("find /c/Users -name '*.tmp' -exec rm -f {} \\;")

    def test_find_delete_dot(self):
        self.no("find . -name '*.tmp' -delete")

    def test_git_clean(self):
        self.no("git clean -fdx")

    def test_git_clean_c_root_subdir_ok_but_dot_not(self):
        self.no("git -C . clean -fd")

    def test_rsync_delete_outside(self):
        self.no("rsync -a --delete src/ /c/Users/x/dst/")

    def test_rsync_remove_source_files_outside(self):
        self.no("rsync -a --remove-source-files /c/Users/x/ srv@192.0.2.49:~/proj/y/")

    def test_shred(self):
        self.no("shred -u /c/Users/x/secret.txt")

    def test_unterminated_quote_falls_back(self):
        self.no("rm -rf '/c/Users/x")

    def test_ps_here_string_python(self):
        self.no("@'\nimport shutil\nshutil.rmtree('/c/Users/x')\n'@ | python -")

    def test_ps_dotnet_delete(self):
        self.no("[System.IO.File]::Delete('C:\\Windows\\x')")

    def test_rclone_delete(self):
        self.no("rclone delete box:proj/x")

    def test_wsl_rm(self):
        self.no("wsl rm -rf /mnt/c/Windows/x")

    def test_stage_lookalike(self):
        self.no("ssh srv@192.0.2.49 'rm -rf /dev/shm/proj-stage-other'")

    def test_stage_dotdot(self):
        self.no("ssh srv@192.0.2.49 'rm -rf /dev/shm/proj-stage/../x'")

    def test_ssh_options_array_closed_host(self):
        self.no("K=(-i /c/Users/x/.ssh/id_rsa -o BatchMode=yes)\n"
                "ssh \"${K[@]}\" ubuntu@203.0.113.1 'cd ~/t && rm -rf work'")

    def test_ssh_options_array_outside_policy(self):
        self.no("K=(-i k); ssh \"${K[@]}\" root@203.0.113.2 'rm -rf /root/tk021/gate'")

    def test_uv_run_python_rmtree(self):
        self.no("uv run python -c \"import shutil; shutil.rmtree('/c/Users/x')\"")

    def test_perl_unlink(self):
        self.no("perl -e 'unlink glob q(/c/Users/x/*)'")

    def test_node_rmsync(self):
        self.no("node -e \"require('fs').rmSync('/c/Users/x', {recursive: true})\"")

    def test_robocopy_mirror(self):
        self.no(r"robocopy empty C:\Users\x /MIR")

    def test_ssh_unknown_options_array_before_closed_host(self):
        self.no("ssh \"${KEY[@]}\" ubuntu@203.0.113.1 'rm -f ~/proj/x'")

    def test_ssh_host_is_unknown_variable(self):
        self.no("ssh $H 'rm -f ~/proj/x'")

    def test_wrapper_script_with_host_arg_outside_policy(self):
        self.no("/tmp/sshx srv@192.0.2.49 'rm -rf /home/srv/other'")

    def test_wrapper_script_closed_host_with_deletion(self):
        self.no("/tmp/sshx ubuntu@203.0.113.1 'rm -f /opt/proj-compute/x'")

    def test_shell_function_wrapper_outside_policy(self):
        self.no("rsh() { ssh -i k srv@192.0.2.49 \"$@\"; }\nrsh 'rm -rf /home/srv/other'")

    def test_sftp_batch_on_closed_host(self):
        self.no("sftp -b - ubuntu@203.0.113.1 <<'EOF'\nrm ~/proj/x\nEOF")

    def test_powershell_script_string_to_closed_host(self):
        self.no("$script = @'\ncd /opt/proj/src && rm -f tools/b5_grid.py\n'@\nssh -i k ubuntu@203.0.113.1 $script")

    def test_powershell_script_piped_to_remote_bash(self):
        self.no("$script = @'\nrm -rf /home/srv/other\n'@\n$script | ssh -i k srv@192.0.2.49 bash -s")

    def test_variable_command_word(self):
        self.no("$SSH srv@192.0.2.49 'rm -rf /home/srv/other'")


class NoNameExemption(unittest.TestCase):
    """A3(а), аудит 03.10: строка `tk020-dedupe-apply.sh` в команде отключала проверку целиком —
    `echo tk020-dedupe-apply.sh; rm -rf /` проходил. Исключения по имени скрипта больше нет."""

    def test_script_name_in_text_does_not_disable_check(self):
        for name in ("tk020-dedupe-apply.sh", "tk020-dedupe-apply.py"):
            self.assertIsNotNone(dg.check(f"echo {name}; rm -rf /c/Users/x", CWD), name)
            self.assertIsNotNone(dg.check(f"rm -rf /c/Users/x # {name}", CWD), name)
            self.assertIsNotNone(
                dg.check(f"ssh srv@192.0.2.49 'python3 {name} && rm -rf /home/srv/other'", CWD), name)

    def test_exemption_machinery_is_gone(self):
        for attr in ("DEDUPE_APPLY", "DEDUPE_UNTIL"):
            self.assertFalse(hasattr(dg, attr), attr)


class Overwrite(unittest.TestCase):
    """A3(б): перезапись/усечение — как удаление, с теми же разрешёнными корнями: `> файл` (не `>>`), `truncate`,
    `dd of=`, `cp`/`mv` поверх существующего вне корней. Внутри своих корней — можно. Существование проверяется
    для локальных буквальных путей (новый файл — не перезапись); удалённый хост/непроверяемый путь — «существует»."""

    @classmethod
    def setUpClass(cls):
        cls.out = outside_dir()                                   # вне корней и вне temp: каталог под домашним
        base = Path(cls.out)
        (base / "f.txt").write_text("x", encoding="utf-8")
        (base / "d").mkdir()
        (base / "d" / "a").write_text("x", encoding="utf-8")
        cls.f = (base / "f.txt").as_posix()
        cls.d = (base / "d").as_posix()
        cls.new = (base / "new.txt").as_posix()                   # не существует

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)               # свой временный каталог

    def no(self, cmd, cwd=CWD):
        self.assertIsNotNone(dg.check(cmd, cwd), f"пропущено: {cmd!r}")

    def ok(self, cmd, cwd=CWD):
        reason = dg.check(cmd, cwd)
        self.assertIsNone(reason, f"ложный отказ: {cmd!r} → {reason}")

    # --- перенаправление `>`
    def test_redirect_truncates_existing_outside(self):
        self.no(f'echo x > "{self.f}"')

    def test_redirect_forms(self):
        self.no(f'echo x >| "{self.f}"')
        self.no(f'echo x &> "{self.f}"')
        self.no(f'echo x 2> "{self.f}"')
        self.no(f'echo x 1>"{self.f}"')

    def test_redirect_without_command(self):
        self.no(f'> "{self.f}"')
        self.no(f': > "{self.f}"')

    def test_heredoc_to_existing_outside(self):
        self.no(f"cat > \"{self.f}\" <<'EOF'\nx\nEOF")

    def test_redirect_after_chain(self):
        self.no(f'ls && echo x > "{self.f}"')

    def test_append_is_not_truncation(self):
        self.ok(f'echo x >> "{self.f}"')
        self.ok(f'echo x 2>> "{self.f}"')

    def test_redirect_to_new_local_file_is_creation(self):
        self.ok(f'echo x > "{self.new}"')

    def test_harmless_targets(self):
        self.ok("ls > /dev/null 2>&1")
        self.ok("ls &>/dev/null")
        self.ok("ls 2>/dev/null")
        self.ok("echo x >/dev/stderr")
        self.ok("ls > $null")
        self.ok("ls >&2")

    def test_redirect_inside_roots_is_fine(self):
        self.ok("echo x > data/out.txt")
        self.ok('echo x > "/c/visual projects/proj/data/out.txt"')
        self.ok(f'echo x > "{SCRATCH}/note.txt"')
        self.ok("ssh srv@192.0.2.49 'echo x > ~/proj/tk026/out.log'")

    def test_redirect_remote_outside_roots(self):
        self.no("ssh srv@192.0.2.49 'echo x > /home/srv/other/f'")
        self.no("ssh ubuntu@203.0.113.2 'ls > /root/out.txt'")

    def test_redirect_unknown_variable_target(self):
        self.no('echo x > "$OUT"')

    def test_redirect_environment_variable_target_is_checked_on_disk(self):
        os.environ["DG_TEST_OUT"] = self.out
        self.addCleanup(lambda: os.environ.pop("DG_TEST_OUT", None))
        self.no('echo x > "$DG_TEST_OUT/f.txt"')                 # существует — перезапись
        self.ok('echo x > "$DG_TEST_OUT/new.txt"')               # нет — создание
        self.ok('echo x > "${DG_TEST_OUT}/new.txt"')
        self.no("cat > \"$DG_TEST_OUT/f.txt\" <<'EOF'\nx\nEOF")
        os.environ.pop("DG_TEST_OUT")
        self.no('echo x > "$DG_TEST_OUT/new.txt"')               # переменная неизвестна — отказ

    def test_redirect_known_variable_target(self):
        self.ok('OUT="/c/visual projects/proj/data/o.txt"; echo x > "$OUT"')

    def test_redirect_to_closed_host_over_ssh(self):
        self.no("ssh ubuntu@203.0.113.1 'cat > ~/proj/x <<EOF\nx\nEOF'")

    # --- truncate
    def test_truncate(self):
        self.no(f'truncate -s 0 "{self.f}"')
        self.no(f'truncate --size=0 "{self.f}"')
        self.no("ssh srv@192.0.2.49 'truncate -s 0 /var/log/x.log'")

    def test_truncate_inside_roots(self):
        self.ok("truncate -s 0 data/x.log")
        self.ok("ssh srv@192.0.2.49 'truncate -s0 ~/proj/x.log'")

    # --- dd
    def test_dd_of(self):
        self.no(f'dd if=/dev/zero of="{self.f}" bs=1 count=1')
        self.no("dd if=/dev/zero of=/dev/sda bs=1M")
        self.no("ssh srv@192.0.2.49 'dd if=a of=/home/srv/other/b'")

    def test_dd_harmless_and_inside(self):
        self.ok("dd if=/dev/zero of=/dev/null bs=1M count=1")
        self.ok("dd if=a of=data/b bs=1M")
        self.ok("dd if=a bs=1M | wc -c")

    # --- cp / mv
    def test_cp_over_existing_file(self):
        self.no(f'cp a.txt "{self.f}"')
        self.no(f'mv a.txt "{self.f}"')
        self.no(f'cp -f a.txt "{self.f}"')

    def test_cp_into_existing_dir_overwrites_member(self):
        self.no(f'cp a "{self.d}"')            # d/a существует
        self.no(f'cp b a "{self.d}/"')
        self.no(f'mv a "{self.d}"')
        self.no(f'cp -t "{self.d}" a')

    def test_cp_into_existing_dir_new_member(self):
        self.ok(f'cp b "{self.d}"')            # d/b нет — это создание
        self.ok(f'mv b "{self.d}/"')

    def test_cp_to_new_path_outside(self):
        self.ok(f'cp a.txt "{self.new}"')

    def test_cp_no_clobber(self):
        self.ok(f'cp -n a.txt "{self.f}"')
        self.ok(f'mv --no-clobber a.txt "{self.f}"')

    def test_cp_inside_roots(self):
        self.ok("cp a.txt data/b.txt")
        self.ok("mv data/a.txt data/b.txt")
        self.ok(f'cp a.txt "{SCRATCH}/b.txt"')
        self.ok("ssh srv@192.0.2.49 'cp a ~/proj/b'")

    def test_cp_remote_outside_roots(self):
        self.no("ssh srv@192.0.2.49 'cp a /home/srv/other/b'")
        self.no("ssh ubuntu@203.0.113.2 'mv a /root/b'")

    def test_cp_unknown_destination(self):
        self.no('cp a "$DEST"')

    def test_cp_through_wrapper_and_find(self):
        self.no(f'sudo cp a "{self.f}"')
        self.no(f"bash -c 'cp a \"{self.f}\"'")
        self.no(f'find data -name "*.x" -exec cp {{}} "{self.f}" \\;')

    def test_find_exec_cp_is_not_deletion_of_find_path(self):
        self.ok(f'find /c/Windows -name "*.log" -exec cp {{}} "{self.new}" \\;')

    def test_cp_without_destination_is_not_overwrite(self):
        self.ok("cp --help")
        self.ok("cp")


class GitIrreversible(unittest.TestCase):
    """A3(в): `git reset --hard`, `git clean -f…`, `git push --force/-f/--force-with-lease` — отказ с причиной
    «только через CEO» (в любом каталоге: это не вопрос пути)."""

    def no(self, cmd, cwd=CWD):
        reason = dg.check(cmd, cwd)
        self.assertIsNotNone(reason, f"пропущено: {cmd!r}")
        self.assertIn("через CEO", reason, cmd)

    def ok(self, cmd, cwd=CWD):
        reason = dg.check(cmd, cwd)
        self.assertIsNone(reason, f"ложный отказ: {cmd!r} → {reason}")

    def test_reset_hard(self):
        self.no("git reset --hard")
        self.no("git reset --hard HEAD~1")
        self.no("git reset HEAD~1 --hard")
        self.no("git -C . reset --hard origin/main")
        self.no("git -c core.x=y reset --hard")

    def test_reset_soft_and_paths_are_fine(self):
        self.ok("git reset --soft HEAD~1")
        self.ok("git reset HEAD file.txt")
        self.ok("git reset --mixed")
        self.ok("git reset")

    def test_clean_force(self):
        self.no("git clean -f")
        self.no("git clean -fd")
        self.no("git clean -fdx")
        self.no("git clean -f -d")
        self.no("git clean --force -d")
        self.no("git clean -fd data/tmp")          # раньше внутри проекта пропускалось
        self.no("git -C data clean -fd")

    def test_clean_dry_run_and_status_are_fine(self):
        self.ok("git clean -n")
        self.ok("git clean -nd")
        self.ok("git clean -nfd")
        self.ok("git clean --dry-run -f")
        self.ok("git status --short")

    def test_push_force(self):
        self.no("git push --force")
        self.no("git push -f")
        self.no("git push --force-with-lease")
        self.no("git push --force-with-lease=main origin main")
        self.no("git push origin main -f")
        self.no("git push -fu origin feature")

    def test_push_plain_is_fine(self):
        self.ok("git push")
        self.ok("git push origin main")
        self.ok("git push -u origin feature")
        self.ok("git push --tags")
        self.ok("git push --follow-tags origin main")

    def test_through_wrappers_and_remote(self):
        self.no("sudo git push -f")
        self.no("bash -c 'git reset --hard'")
        self.no("ssh srv@192.0.2.49 'cd ~/proj && git clean -fd'")
        self.no("echo ok && git push --force")
        self.no('echo "$(git reset --hard)"')

    def test_text_mentions_are_not_commands(self):
        self.ok('git commit -m "не делать git reset --hard и git push --force"')
        self.ok("echo git clean -fd")
        self.ok("grep -n 'git push -f' README.md")


class CeoResources0310(unittest.TestCase):
    """Решение CEO 03.10 по ресурсам: (1) закрытые и неопределённые хосты, (2) git reset --hard / clean -f вне основного
    дерева, (3) автопамять проекта — запись/перезапись."""

    WT = SCRATCH + "/wt"
    MEM = "C--visual-projects-proj/memory"

    def ok(self, cmd, cwd=CWD):
        reason = dg.check(cmd, cwd)
        self.assertIsNone(reason, f"ложный отказ: {cmd!r} → {reason}")

    def no(self, cmd, cwd=CWD):
        self.assertIsNotNone(dg.check(cmd, cwd), f"пропущено: {cmd!r}")

    # --- (1) закрытые хосты и хост, который не определить, закрыты и для временных каталогов
    def test_closed_and_unknown_hosts_stay_closed(self):
        self.no("ssh root@203.0.113.1 'rm -rf /tmp/x'")                 # закрытый хост: и /tmp/ нельзя
        self.no("ssh $H 'rm -rf /tmp/x'")                                # хост не определён
        self.no("ssh root@203.0.113.2 'ssh root@203.0.113.1 rm -rf /tmp/x'")   # вложенный ssh — уже другой хост
        self.no("rm -rf /root/tk031/x")                                  # локально чужой каталог

    # --- (2) git reset --hard / clean -f вне основного дерева
    def test_git_in_scratch_dir_by_cd_variable_and_git_c(self):
        self.ok(f'cd "{self.WT}" && git reset -q --hard abc123')
        self.ok(f'W="{self.WT}" && cd "$W" && git reset -q --hard abc123 && git apply t.patch')
        self.ok(f'cd "C:/visual projects/proj" && W="{self.WT}" && git diff > "$W/t.patch" && cd "$W" && git reset --hard d1')
        self.ok(f'git -C "{self.WT}" reset --hard HEAD~1')
        self.ok(f'git -C "{self.WT}" clean -fdx')
        self.ok("git reset --hard", cwd=self.WT.replace("/", "\\"))      # рабочий каталог вызова — scratch
        self.ok("git clean -fd", cwd=self.WT)

    def test_git_in_remote_and_tmp_dirs(self):
        self.ok("ssh root@203.0.113.2 'cd /opt/proj-compute/wt1 && git reset --hard X && git clean -fdx'")
        self.ok("ssh root@203.0.113.2 'git -C /opt/proj-compute/wt1 reset --hard X'")
        self.ok("ssh root@203.0.113.2 'cd /tmp/wt && git clean -fd'")
        self.ok("cd /tmp/wt && git clean -fd")

    def test_git_main_tree_still_refused(self):
        self.no('cd "C:/visual projects/proj" && git reset --hard')
        self.no('cd "C:/visual projects/proj/data" && git clean -fd')
        self.no('cd "C:/visual projects/proj/.claude/wt" && git reset --hard')
        self.no('git -C "C:/visual projects/proj" reset --hard', cwd=self.WT)
        self.no('git -C "/c/visual projects/proj" clean -fd', cwd=self.WT)
        self.no("git reset --hard")                                       # рабочий каталог — основное дерево
        self.no(f'git -C "{self.WT}/../../../../../../../../visual projects/proj" reset --hard')

    def test_git_unknown_or_indirect_dir_refused(self):
        self.no('cd $W && git reset --hard')                              # неизвестная переменная
        self.no('cd "$W" && git clean -fd')
        self.no('W=$(mktemp -d); cd "$W" && git reset --hard')
        self.no('git -C "$W" reset --hard', cwd=self.WT)
        self.no(f'GIT_DIR="C:/visual projects/proj/.git" git -C "{self.WT}" reset --hard')
        self.no(f'export GIT_WORK_TREE="C:/visual projects/proj"; cd "{self.WT}" && git reset --hard')
        self.no('git --git-dir="C:/visual projects/proj/.git" reset --hard', cwd=self.WT)
        self.no('git --work-tree="C:/visual projects/proj" reset --hard', cwd=self.WT)
        self.no('cd wt && git reset --hard', cwd=None)                    # относительный путь без известного cwd

    def test_git_remote_roots_themselves_and_other_places_refused(self):
        self.no("ssh srv@192.0.2.49 'cd ~/proj && git clean -fd'")
        self.no("ssh root@203.0.113.2 'cd /opt/proj-compute && git reset --hard'")
        self.no("ssh root@203.0.113.2 'cd /root/x && git reset --hard'")
        self.no("ssh root@203.0.113.2 'cd /tmp && git reset --hard'")
        self.no("cd /tmp && git reset --hard")
        self.no("ssh root@203.0.113.1 'cd /tmp/wt && git reset --hard'")      # закрытый хост
        self.no("ssh root@203.0.113.2 'cd /tmp/wt/../../root/x && git clean -fd'")

    def test_git_push_force_always_refused(self):
        self.no(f'cd "{self.WT}" && git push --force')
        self.no("git push -f", cwd=self.WT)
        self.no("ssh root@203.0.113.2 'cd /opt/proj-compute/wt1 && git push --force-with-lease'")

    def test_git_linked_worktree_inside_project_allowed_nested_repo_and_main_not(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(prefix="dg-proj-") as d:
            root = Path(d)
            (root / ".git").mkdir()
            (root / "data").mkdir()
            (root / "wt" / "sub").mkdir(parents=True)
            (root / "wt" / ".git").write_text("gitdir: ../.git/worktrees/wt", encoding="utf-8")   # связанный worktree
            (root / "nested" / ".git").mkdir(parents=True)                                       # вложенный репозиторий
            (root / ".claude" / "worktrees" / "rpv" / ".git").mkdir(parents=True)                    # TK-070 п.5: клон плагина
            base = dg.norm(root.as_posix())
            with mock.patch.object(dg, "LOCAL_ROOTS", (base + "/",)):
                self.ok(f'cd "{base}/wt" && git reset --hard X')
                self.ok(f'cd "{base}/wt/sub" && git clean -fd')
                self.ok(f'git -C "{base}/wt" reset --hard X')
                self.no(f'cd "{base}" && git reset --hard X')
                self.no(f'cd "{base}/data" && git clean -fd')
                self.ok(f'cd "{base}/.claude/worktrees/rpv" && git reset --hard X')                     # клон плагина — разрешён
                self.no(f'cd "{base}/nested" && git reset --hard X')
                self.no(f'cd "{base}/ghost" && git reset --hard X')                               # нет такого каталога
                self.no(f'cd "{base}/wt" && git push --force')
                self.no(f"ssh root@203.0.113.2 'cd {base}/wt && git reset --hard X'")           # не локальная ФС

    def test_git_refusal_message_explains_the_rule(self):
        reason = dg.check("git reset --hard", CWD)
        self.assertIn("через CEO", reason)
        self.assertIn("явным путём", reason)

    # --- (3) автопамять: запись/перезапись, не удаление
    def test_memory_overwrite_allowed(self):
        for base in ("~/.claude/projects/" + self.MEM, "$HOME/.claude/projects/" + self.MEM,
                     '"$HOME/.claude/projects/' + self.MEM + '"', "/c/Users/user/.claude/projects/" + self.MEM,
                     "C:/Users/user/.claude/projects/" + self.MEM,
                     "C:\\Users\\user\\.claude\\projects\\C--visual-projects-proj\\memory"):
            self.ok(f"echo x > {base}/MEMORY.md")
            self.ok(f"echo x >| {base}/proj-state.md")
        home = "~/.claude/projects/" + self.MEM
        self.ok(f"cp /c/tmp/new.md {home}/x.md")
        self.ok(f"mv data/new.md {home}/x.md")
        self.ok(f"truncate -s 0 {home}/x.md")
        self.ok(f"dd if=a of={home}/x.md")
        self.ok(f"cd {home} && echo x > MEMORY.md")

    def test_memory_deletion_other_projects_and_remote_stay_closed(self):
        home = "~/.claude/projects/" + self.MEM
        self.no(f"rm {home}/x.md")                                        # только запись, удаление — нет
        self.no(f"rm -rf {home}")
        self.no("ssh srv@192.0.2.49 'echo x > ~/.claude/projects/" + self.MEM + "/x.md'")   # удалённый ~ — другой
        with tempfile.TemporaryDirectory(prefix="dg-mem-", dir=str(Path.home())) as d:
            base = Path(d) / ".claude" / "projects"
            for proj in ("c--other-project", "c--visual-projects-proj"):
                (base / proj / "memory").mkdir(parents=True)
                (base / proj / "memory" / "x.md").write_text("x", encoding="utf-8")
            # существующие файлы: чужая автопамять и та же, но не под домашним каталогом — отказ
            self.no(f"echo x > {(base / 'c--other-project' / 'memory' / 'x.md').as_posix()}")
            self.no(f"echo x > {(base / 'c--visual-projects-proj' / 'memory' / 'x.md').as_posix()}")
        self.assertFalse(dg.write_only_ok("~/.claude/projects/C--visual-projects-proj/memory/../../../settings.json"))
        self.assertFalse(dg.write_only_ok("~/.claude/projects/C--visual-projects-proj/memory"))   # сам каталог — нет
        self.no("ssh root@203.0.113.2 'echo x > ~/.claude/settings.json'")


class SecondAudit(unittest.TestCase):
    """Второй проход аудита 03.10 (CEO): (а) `mv` — источник как удаление; (б) Write/Edit/MultiEdit/NotebookEdit;
    (в) `tee`, `find -exec`, git checkout/restore/switch/branch -D/stash/push+delete; (г) fail-closed; (д) temp."""

    @classmethod
    def setUpClass(cls):
        cls.out = outside_dir()                                   # вне корней и вне temp: каталог под домашним
        base = Path(cls.out)
        (base / "f.txt").write_text("x", encoding="utf-8")
        (base / "d").mkdir()
        (base / "d" / "a").write_text("x", encoding="utf-8")
        cls.f = (base / "f.txt").as_posix()
        cls.d = (base / "d").as_posix()
        cls.new = (base / "new.txt").as_posix()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def setUp(self):
        from unittest import mock
        p = mock.patch.dict(os.environ, {"RPV_ROLE": "engineer"})   # файловые правила этого прохода — для запуска диспетчера
        p.start()
        self.addCleanup(p.stop)

    def no(self, cmd, cwd=CWD):
        self.assertIsNotNone(dg.check(cmd, cwd), f"пропущено: {cmd!r}")

    def ok(self, cmd, cwd=CWD):
        reason = dg.check(cmd, cwd)
        self.assertIsNone(reason, f"ложный отказ: {cmd!r} → {reason}")

    def no_ceo(self, cmd, cwd=CWD):
        reason = dg.check(cmd, cwd)
        self.assertIsNotNone(reason, f"пропущено: {cmd!r}")
        self.assertIn("через CEO", reason, cmd)

    # ------------------------------------------------------------------------------------------ (а) mv: источник
    def test_mv_of_protected_source_is_refused_at_the_move(self):
        with protect("data/raw", "keep"):
            self.no('mv "/c/visual projects/proj/data/raw/x.bin" ./trash && rm -rf ./trash')
            self.no("mv data/raw/x.bin ./trash && rm -rf ./trash")
            self.no("mv data/raw data/old && ls")
            self.no("ssh srv@192.0.2.49 'mv ~/proj/e1/keep/x ~/proj/trash'")
            self.ok("mv data/other.bin data/old.bin")

    def test_mv_source_outside_roots(self):
        self.no("mv /c/Users/x/a.bin data/")
        self.no("mv -n /c/Users/x/a.bin data/new.bin")                # -n охраняет приёмник, источник всё равно исчезает
        self.no("mv -t data/old /c/Users/x/a data/b")
        self.no("mv data/a /c/Users/x/b data/old/")                   # хотя бы один источник вне папки
        self.no("sudo mv /c/Users/x/a data/")
        self.no("bash -c 'mv /c/Users/x/a data/'")
        self.no("ssh srv@192.0.2.49 'mv /home/srv/other/a ~/proj/x'")

    def test_mv_unknown_or_stdin_sources(self):
        self.no('mv "$f" data/')
        self.no("ls | xargs mv -t data/old")
        self.no("ls data/tmp | xargs -I{} mv {} data/old/")
        self.no("mv $(cat list.txt) data/old/")

    def test_mv_inside_roots_is_fine(self):
        self.ok("mv data/a.bin data/b.bin")
        self.ok("mv -t data/old data/a data/b")
        self.ok("mv data/tmp/*.csv data/old/")
        self.ok('mv "/c/visual projects/proj/data/a" "/c/visual projects/proj/data/b"')
        self.ok(f'mv data/a "{SCRATCH}/b"')
        self.ok("ssh srv@192.0.2.49 'mv ~/proj/tk026/a ~/proj/tk026/b'")
        self.ok("mv --help")

    def test_cp_source_is_only_read(self):
        self.ok("cp /c/Users/x/a.bin data/new.bin")
        self.ok(f'cp "{self.f}" data/f.txt')

    def test_find_exec_mv(self):
        self.no(f'find "{self.d}" -name "a" -exec mv {{}} data/old \\;')
        self.no("find /c/Users/x -name '*.t' -exec mv {} data/old/ \\;")
        self.ok("find data/tmp -name '*.part' -exec mv {} data/old/ \\;")

    def test_powershell_move_and_rename(self):
        self.no("Move-Item C:\\Users\\x\\a.txt data\\b.txt")
        self.no("Move-Item -Path C:\\Users\\x\\a.txt -Destination data\\b.txt")
        self.no("mi C:\\Users\\x\\a.txt data\\b.txt")
        self.no("move C:\\Users\\x\\a.txt data\\b.txt")
        with protect("data/raw"):
            self.no("Move-Item data\\raw\\a.txt data\\b.txt")
        self.no("Rename-Item C:\\Users\\x\\a.txt b.txt")
        self.no("ren C:\\Users\\x\\a.txt b.txt")
        self.ok("Move-Item data\\a.txt data\\b.txt")
        self.ok("Move-Item -Path data\\a.txt -Destination data\\b.txt -Force")
        self.ok("Rename-Item data\\a.txt b.txt")
        self.ok("Move-Item C:\\Users\\x\\a.txt data\\b.txt -WhatIf")

    def test_powershell_copy_overwrite(self):
        self.no(f'Copy-Item data\\a.txt "{self.f}"')
        self.no(f'Copy-Item -Path data\\a.txt -Destination "{self.f}" -Force')
        self.no(f'Move-Item data\\a.txt "{self.f}"')
        self.ok("Copy-Item C:\\Users\\x\\a.txt data\\b.txt")            # источник Copy-Item только читается
        self.ok(f'Copy-Item data\\a.txt "{self.new}"')

    # ------------------------------------------------------------------------------------------ (б) файловые инструменты
    def tool(self, name, path, cwd=CWD, key=None):
        key = key or ("notebook_path" if name == "NotebookEdit" else "file_path")
        return dg.check_tool({"tool_name": name, "tool_input": {key: path}, "cwd": cwd})

    def test_file_tools_existing_outside_refused(self):
        for name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            reason = self.tool(name, self.f)
            self.assertIsNotNone(reason, name)
            self.assertIn("вне своей папки", reason)
        self.assertIsNotNone(self.tool("Write", self.f.replace("/", "\\")))     # путь Windows с обратными чертами

    def test_file_tools_new_file_outside_is_creation(self):
        for name in ("Write", "Edit", "NotebookEdit"):
            self.assertIsNone(self.tool(name, self.new), name)

    def test_file_tools_inside_roots(self):
        for path in ("C:\\visual projects\\proj\\src\\lob\\x.rs", "C:/visual projects/proj/docs/a.md",
                     "/c/visual projects/proj/.claude/roles/notes/ceo.md", f"{SCRATCH}/p.py", "data/out.txt"):
            self.assertIsNone(self.tool("Write", path), path)
            self.assertIsNone(self.tool("Edit", path), path)
        self.assertIsNone(self.tool("MultiEdit", "src/lob/x.rs", cwd="C:/visual projects/proj"))

    def test_file_tools_memory_and_temp(self):
        mem = "C:/Users/x/.claude/projects/C--visual-projects-proj/memory/MEMORY.md"
        self.assertIsNone(self.tool("Write", mem))
        self.assertIsNone(self.tool("Edit", "C:/Users/x/AppData/Local/Temp/anything/f.txt"))
        self.assertIsNone(self.tool("Write", "/tmp/note.txt"))

    def test_file_tools_without_path_or_odd_input_refused(self):
        self.assertIsNotNone(dg.check_tool({"tool_name": "Write", "tool_input": {"content": "x"}, "cwd": CWD}))
        self.assertIsNotNone(dg.check_tool({"tool_name": "Edit", "tool_input": None, "cwd": CWD}))
        self.assertIsNotNone(dg.check_tool({"tool_name": "Write", "tool_input": {"file_path": "  "}, "cwd": CWD}))
        self.assertIsNotNone(self.tool("Write", self.f, key="path"))                 # запасное имя ключа тоже читается

    def test_check_tool_routing(self):
        self.assertIsNone(dg.check_tool({"tool_name": "Read", "tool_input": {"file_path": self.f}, "cwd": CWD}))
        self.assertIsNone(dg.check_tool({"tool_name": "Grep", "tool_input": {}, "cwd": CWD}))
        self.assertIsNotNone(dg.check_tool({"tool_name": "Bash", "tool_input": {"command": "rm -rf /c/Users/x"}, "cwd": CWD}))
        self.assertIsNotNone(dg.check_tool({"tool_name": "PowerShell", "tool_input": {"command": "del C:\\x"}, "cwd": CWD}))
        self.assertIsNone(dg.check_tool({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": CWD}))
        self.assertIsNone(dg.check_tool({"tool_name": "Bash", "tool_input": {}, "cwd": CWD}))

    # ------------------------------------------------------------------------------------------ (в) tee
    def test_tee_truncates(self):
        self.no(f'echo x | tee "{self.f}"')
        self.no(f'echo x | tee -p "{self.f}"')
        self.no(f'echo x | sudo tee "{self.f}"')
        self.no(f'echo x | tee "{self.f}" data/o.txt')
        self.no('echo x | tee "$OUT"')
        self.no("ssh srv@192.0.2.49 'echo x | tee /home/srv/other/f'")
        self.no(f"bash -c 'echo x | tee \"{self.f}\"'")

    def test_tee_append_new_inside_harmless_are_fine(self):
        self.ok(f'echo x | tee -a "{self.f}"')
        self.ok(f'echo x | tee --append "{self.f}"')
        self.ok(f'echo x | tee -ia "{self.f}"')
        self.ok(f'echo x | tee "{self.new}"')
        self.ok("echo x | tee data/out.txt")
        self.ok("echo x | tee /dev/null")
        self.ok("echo x | tee")
        self.ok("ssh srv@192.0.2.49 'echo x | tee ~/proj/tk026/o.log'")

    def test_powershell_writers(self):
        self.no(f'"x" | Out-File "{self.f}"')
        self.no(f'"x" | Out-File -FilePath "{self.f}" -Encoding utf8')
        self.no(f'Set-Content -Path "{self.f}" -Value x')
        self.no(f'Set-Content "{self.f}" x')
        self.no(f'sc "{self.f}" x')
        self.no(f'Clear-Content "{self.f}"')
        self.no(f'Get-Date | Tee-Object -FilePath "{self.f}"')
        self.no(f'Get-Date | Tee-Object "{self.f}"')
        self.ok(f'"x" | Out-File "{self.f}" -Append')
        self.ok(f'"x" | Out-File "{self.f}" -NoClobber')
        self.ok(f'Set-Content "{self.f}" x -WhatIf')
        self.ok(f'Get-Date | Tee-Object -Variable v')
        self.ok(f'Set-Content "{self.new}" x')
        self.ok('"x" | Out-File data\\o.txt')
        self.ok('Set-Content -Path data\\o.txt -Value x')

    def test_powershell_variable_without_spaces_is_tracked(self):
        self.ok('$out="data\\dashboard"; Set-Content "$out\\x.pid" 1')
        self.no(f'$out="{self.out}"; Set-Content "$out\\f.txt" 1')

    # ------------------------------------------------------------------------------------------ (в) find -exec
    def test_find_exec_overwrites_found_files(self):
        self.no(f'find "{self.d}" -name a -exec cp b {{}} \\;')
        self.no(f'find "{self.d}" -name a -exec truncate -s0 {{}} \\;')
        self.no(f'find "{self.d}" -name a -exec tee {{}} \\;')
        self.no(f'find "{self.d}" -name a -execdir cp b {{}} +')
        self.no(f'find data -name "*.x" -exec cp {{}} "{self.d}/" \\;')       # члены каталога вне папки неизвестны
        self.no(f'find data -name "*.x" -exec cp {{}} "{self.d}/{{}}" \\;')
        self.no(f'find data -name "*.x" -exec cp -t "{self.d}" {{}} +')
        self.no(f'find data -fprint "{self.f}"')
        self.no(f'find data -fprintf "{self.f}" "%p\\n"')
        self.no("find /c/Users/x -name '*.t' -exec sh -c 'echo x > \"$1\"' _ {} \\;")

    def test_find_exec_inside_roots_is_fine(self):
        self.ok("find data -name '*.x' -exec cp a {} \\;")
        self.ok("find data -name '*.x' -exec cp {} data/old/ \\;")
        self.ok("find data -name '*.x' -exec cp {} data/old/{} \\;")
        self.ok("find data -fprint data/list.txt")
        self.ok(f'find "{self.d}" -name a -exec cp {{}} data/old/ \\;')           # чтение из внешнего каталога
        self.ok(f'find "{self.d}" -name a -exec grep -l x {{}} \\;')
        self.ok(f'find "{self.d}" -name a -print')

    # ------------------------------------------------------------------------------------------ (в) git
    def test_git_discard_commands_refused(self):
        for cmd in ("git checkout -- .", "git checkout -- src/x.rs", "git checkout .", "git checkout HEAD -- f",
                    "git checkout HEAD~1 f", "git checkout stash@{0} -- f", "git checkout -f", "git checkout -f main",
                    "git checkout -p", "git checkout --ours -- x", "git checkout *.rs", "git -C . checkout -- x",
                    "git -c core.x=y checkout -- x", "git restore x", "git restore .", "git restore --worktree x",
                    "git restore -SW x", "git restore --source=HEAD~1 x", "git restore -p",
                    "git switch -f main", "git switch --discard-changes main", "git switch --force main",
                    "git branch -D x", "git branch -d -f x", "git branch -fd x", "git branch --delete --force x",
                    "git branch -M new", "git branch -f main HEAD~1", "git branch -C a b",
                    "git stash clear", "git stash drop", "git stash drop stash@{1}",
                    "sudo git checkout -- x", "bash -c 'git restore x'", 'echo "$(git stash drop)"',
                    "ssh srv@192.0.2.49 'cd ~/proj && git checkout -- x'", "echo ok && git branch -D x"):
            self.no_ceo(cmd)

    def test_git_push_plus_and_delete_refused(self):
        for cmd in ("git push origin +main", "git push origin +HEAD:main", "git push origin :old",
                    "git push origin --delete old", "git push -d origin old", "git push --mirror",
                    "git push --prune origin", "git push origin main --delete"):
            self.no_ceo(cmd)

    def test_git_safe_forms_are_fine(self):
        for cmd in ("git checkout main", "git checkout feature/x", "git checkout -b feature", "git checkout -B f origin/main",
                    "git checkout -", "git checkout --detach HEAD", "git checkout -q main", "git switch main",
                    "git switch -c new", "git switch -", "git restore --staged x", "git restore -S .",
                    "git restore --staged --source=HEAD x", "git branch", "git branch -a", "git branch -vv",
                    "git branch -d merged", "git branch new-branch", "git branch --list", "git branch -r",
                    "git stash", "git stash list", "git stash pop", "git stash push -m x", "git stash apply",
                    "git push origin HEAD:refs/heads/x", "git push -u origin feature",     # main и теги — публикации: Publication
                    "git checkout --help", "git restore --help"):
            self.ok(cmd)

    def test_git_text_mentions_are_not_commands(self):
        self.ok('git commit -m "не делать git checkout -- . и git stash drop и git branch -D"')
        self.ok("echo git restore .")
        self.ok("grep -n 'git checkout -- ' README.md")
        self.ok("rg 'git push --delete' docs")

    def test_git_discard_allowed_in_scratch_repos_only(self):
        wt = SCRATCH + "/wt"
        for cmd in ("git checkout -- .", "git restore .", "git switch -f main", "git branch -D tmp", "git stash drop",
                    "git stash clear"):
            self.ok(f'cd "{wt}" && {cmd}')
            self.ok(f'git -C "{wt}" {cmd[4:]}')
            self.ok(cmd, cwd=wt)
            self.no(cmd)                                                   # основное дерево
            self.no(f'cd "C:/visual projects/proj" && {cmd}')
        self.ok("ssh root@203.0.113.2 'cd /opt/proj-compute/wt1 && git checkout -- . && git stash drop'")
        self.no("ssh root@203.0.113.2 'cd /opt/proj-compute && git checkout -- .'")
        self.no(f'cd "{wt}" && git push origin +main')                    # push — всегда через CEO
        self.no(f'cd "{wt}" && git push origin --delete x')

    def test_git_checkout_existing_path_without_double_dash(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(prefix="dg-proj-") as d:
            base = dg.norm(Path(d).as_posix())
            (Path(d) / "tracked.txt").write_text("x", encoding="utf-8")
            with mock.patch.object(dg, "LOCAL_ROOTS", (base + "/",)):
                self.no("git checkout tracked.txt", cwd=base)                 # такой файл есть: это восстановление пути
                self.ok("git checkout feature-x", cwd=base)                   # файла нет: переключение ветки

    # ------------------------------------------------------------------------------------------ (г) fail-closed
    def test_crash_in_scan_is_refusal_not_pass(self):
        from unittest import mock
        with mock.patch.object(dg, "scan_text", side_effect=RuntimeError("boom")):
            reason = dg.check("ls", CWD)
        self.assertIsNotNone(reason)
        self.assertIn("Страж удаления упал", reason)
        self.assertIn("RuntimeError", reason)

    def test_crash_in_target_check_is_refusal_not_pass(self):
        from unittest import mock
        with mock.patch.object(dg, "allowed", side_effect=ValueError("bad")):
            self.assertIn("Страж удаления упал", dg.check("rm -rf data/x", CWD))

    def test_crash_in_legacy_fallback_is_refusal(self):
        from unittest import mock
        with mock.patch.object(dg, "legacy_found", side_effect=RuntimeError("boom")):
            self.assertIn("Страж удаления упал", dg.check("echo 'unterminated", CWD))

    def test_crash_in_check_tool_is_refusal(self):
        from unittest import mock
        ev = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": CWD}
        with mock.patch.object(dg, "check", side_effect=RuntimeError("boom")):
            self.assertIn("Страж удаления упал", dg.check_tool(ev))
        ev = {"tool_name": "Write", "tool_input": {"file_path": self.f}, "cwd": CWD}
        with mock.patch.object(dg, "may_exist", side_effect=RuntimeError("boom")):
            self.assertIn("Страж удаления упал", dg.check_tool(ev))
        self.assertIn("Страж удаления упал", dg.check_tool("не объект"))
        self.assertIn("Страж удаления упал", dg.check_tool(None))

    def test_unterminated_quote_still_uses_legacy_regex(self):
        self.no("rm -rf '/c/Users/x")
        self.ok("echo 'abc")

    # ------------------------------------------------------------------------------------------ (д) временные файлы
    def test_temp_variables_and_tmp_allowed(self):
        for target in ("$TMP/x", "$TEMP/x.log", "${TMPDIR}/a/b", "${TMPDIR:-/tmp}/x", "$env:TEMP\\x.txt",
                       "$env:TMP/y", "%TEMP%\\x", "/tmp/x", "/tmp/judge/a.log", "$TMP/claude/s/out.txt",
                       "C:/Users/x/AppData/Local/Temp/f.txt", "/c/Users/x/AppData/Local/Temp/dir/f", "C:\\Users\\3D6B~1\\AppData\\Local\\Temp\\f"):
            self.ok(f'echo x > "{target}"')
            self.ok(f'rm -rf "{target}"')
            self.ok(f'truncate -s 0 "{target}"')
            self.ok(f'cp data/a.txt "{target}"')
            self.ok(f'mv "{target}" data/')
            self.ok(f'echo x | tee "{target}"')

    def test_temp_with_existing_files_is_not_data(self):
        t = Path(tempfile.mkdtemp(prefix="dg-temp-"))                        # настоящий системный temp, файл уже есть
        self.addCleanup(shutil.rmtree, t, True)
        (t / "f.txt").write_text("x", encoding="utf-8")
        self.ok(f'echo x > "{(t / "f.txt").as_posix()}"')
        self.ok(f'rm -f "{(t / "f.txt").as_posix()}"')
        self.ok(f'cp data/a.txt "{(t / "f.txt").as_posix()}"')
        self.assertIsNone(self.tool("Write", (t / "f.txt").as_posix()))
        self.assertIsNone(self.tool("Edit", str(t / "f.txt")))

    def test_temp_in_remote_hosts(self):
        for host in ("root@203.0.113.2", "srv@192.0.2.49"):
            self.ok(f"ssh {host} 'rm -rf /tmp/x'")
            self.ok(f"ssh {host} 'echo x > /tmp/dbg.out'")
            self.ok(f"ssh {host} 'cd /tmp/work && rm -rf out'")
            self.ok(f"ssh {host} 'echo x > $TMP/y; rm -f $TMPDIR/y'")
            self.no(f"ssh {host} 'rm -rf /tmp'")
            self.no(f"ssh {host} 'rm -rf /tmp/*'")
            self.no(f"ssh {host} 'rm -rf /tmp/../etc/x'")

    def test_temp_not_in_closed_nodes(self):
        self.no("ssh root@203.0.113.1 'rm -rf /tmp/x'")                      # закрытый хост
        self.no("ssh root@203.0.113.1 'echo x > /tmp/x'")
        self.no("ssh $H 'rm -rf /tmp/x'")                                     # хост не определён
        self.no("ssh $H 'echo x > $TMP/x'")

    def test_temp_roots_themselves_dotdot_and_glob_stay_closed(self):
        for target in ("$TMP", "$TEMP/", "/tmp", "/tmp/", "/tmp/*", "$TMP/*", "$TMP/../x", "/tmp/../etc/x",
                       "/tmp/x/../../etc", "$TEMP/..", "C:/Users/x/AppData/Local/Temp/../Documents/f",
                       "${TMP}/../x", "$env:TEMP\\..\\x"):
            self.no(f'rm -rf "{target}"')
        self.no("cd /tmp && rm -rf *")
        self.ok("cd /tmp && rm -rf data")

    def test_temp_known_variable_overrides_environment(self):
        self.no("TMP=/c/Users/x; rm -rf $TMP/y")                              # переменная переопределена в самой команде
        self.no(f'TEMP="{self.out}"; echo x > "$TEMP/f.txt"')                 # файл существует — перезапись вне папки
        self.ok('TMP=/tmp/work; rm -rf "$TMP/y"')

    def test_mktemp_results(self):
        self.ok("rm -rf $(mktemp -d)/a")
        self.ok('echo x > "$(mktemp -d)/log"')
        self.ok("T=$(mktemp); echo x > $T; rm -f $T")
        self.ok('W=$(mktemp -d); echo x > "$W/o.txt"; rm -rf "$W/o.txt"')
        self.ok('W=$(mktemp -d /tmp/w.XXXX) && cd "$W" && rm -rf stage')
        self.ok('W=$(mktemp -d) && cd "$W" && echo x > f.txt')
        self.ok('W=`mktemp -d`; rm -rf $W/x')
        self.ok('cp a.txt "$(mktemp -d)/b"')
        self.ok("rm -rf $(mktemp -d)")                                        # только что созданный временный каталог
        self.ok("ssh srv@192.0.2.49 'W=$(mktemp -d); rm -rf $W/x'")
        self.ok("mktemp -d")

    def test_mktemp_with_explicit_directory_is_not_temp(self):
        self.no("rm -rf $(mktemp -d -p /c/Users/x)/a")
        self.no("rm -rf $(mktemp -d --tmpdir=/c/Users/x)/a")
        self.no("rm -rf $(mktemp -d /c/Users/x/w.XXXX)/a")
        self.no('W=$(mktemp -d -p /c/Users/x); rm -rf "$W/a"')
        self.no("rm -rf $(mktemp -d)/../a")
        self.no("ssh root@203.0.113.1 'W=$(mktemp -d); rm -rf $W/x'")

    def test_temp_reason_text_and_other_users_dirs_still_closed(self):
        self.no("rm -rf C:/Users/x/Documents/f")
        self.no("rm -rf C:/Users/x/AppData/Local/Programs/f")
        self.no("rm -rf C:/Users/x/AppData/Roaming/f")


class ThirdAudit(unittest.TestCase):
    """Третий проход аудита 03.10: (е) файловые инструменты вне проекта ограничены только для запусков диспетчера;
    (ж) `.git` — удаление никому, запись — не запускам диспетчера; (з) настройки Claude Code — не запускам диспетчера."""

    @classmethod
    def setUpClass(cls):
        cls.out = outside_dir()
        base = Path(cls.out)
        (base / "settings.json").write_text("{}", encoding="utf-8")
        (base / "repo").mkdir()
        (base / "repo" / "main.py").write_text("x", encoding="utf-8")
        cls.settings = (base / "settings.json").as_posix()
        cls.repo_file = (base / "repo" / "main.py").as_posix()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def as_role(self, role):
        from unittest import mock
        env = {"RPV_ROLE": role} if role else {}
        p = mock.patch.dict(os.environ, env)
        p.start()
        if not role:
            os.environ.pop("RPV_ROLE", None)
            os.environ.pop("ALPHA_ROLE", None)
        self.addCleanup(p.stop)

    def tool(self, name, path, cwd=CWD):
        key = "notebook_path" if name == "NotebookEdit" else "file_path"
        return dg.check_tool({"tool_name": name, "tool_input": {key: path}, "cwd": cwd})

    # ------------------------------------------------------------------------------------------ (е) чьи правила
    def test_ceo_session_edits_outside_project(self):
        self.as_role(None)
        for name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            self.assertIsNone(self.tool(name, self.settings), name)          # ~/.claude/settings.json и т. п.
            self.assertIsNone(self.tool(name, self.repo_file), name)         # соседний репозиторий (сам плагин)

    def test_dispatcher_role_keeps_full_rule(self):
        for role in ("engineer", "researcher", "judge"):
            self.as_role(role)
            self.assertIsNotNone(self.tool("Edit", self.repo_file), role)
            self.assertIn("вне своей папки", self.tool("Write", self.settings), role)

    def test_unknown_role_value_is_ceo_session(self):
        self.as_role("ceo")                                                # не роль диспетчера — правила сессии CEO
        self.assertIsNone(self.tool("Edit", self.repo_file))

    # ------------------------------------------------------------------------------------------ (ж) .git
    def test_git_dir_deletion_refused_for_everyone(self):
        for role in (None, "engineer"):
            self.as_role(role)
            for cmd in ("rm -rf .git", "rm -rf ./.git/", 'rm -rf "C:/visual projects/proj/.git"', "rm -rf .*",
                        "rm -rf .[!.]*", "rm -rf .g*", "rm -f .git/config", "rm -rf .git/objects",
                        "mv .git /c/Users/x/old-git", "cd .git && rm -rf objects", "find .git -delete",
                        "ssh srv@192.0.2.49 'rm -rf ~/proj/repo/.git'", "Remove-Item -Recurse .git"):
                reason = dg.check(cmd, CWD)
                self.assertIsNotNone(reason, f"{role}: {cmd}")
            self.assertIn(".git", dg.check("rm -rf .git", CWD))

    def test_git_dir_neighbours_and_locks_allowed(self):
        self.as_role("engineer")
        for cmd in ("rm -f .git/index.lock", "rm -f .git/refs/heads/main.lock", "rm -rf .github/old",
                    "rm -f .gitignore.bak", "rm -rf node_modules/.cache", "rm -rf .claude/worktrees/agent-1",
                    "rm -f .*.swp", "rm -rf /tmp/clone/.git", "git status", "echo x >> .git/info/exclude"):
            self.assertIsNone(dg.check(cmd, CWD), cmd)

    def test_git_dir_writes(self):
        self.as_role("engineer")
        self.assertIsNotNone(self.tool("Edit", "C:/visual projects/proj/.git/config"))
        self.assertIsNotNone(dg.check("echo ref > .git/HEAD", CWD))
        self.as_role(None)                                                 # CEO: правка .git/hooks и т. п. — можно
        self.assertIsNone(self.tool("Edit", "C:/visual projects/proj/.git/hooks/pre-commit"))
        self.assertIsNone(dg.check("echo ref > .git/info/exclude", CWD))

    # ------------------------------------------------------------------------------------------ (з) настройки
    def test_settings_closed_to_dispatcher_roles(self):
        self.as_role("engineer")
        for name in ("Write", "Edit", "MultiEdit"):
            for path in ("C:/visual projects/proj/.claude/settings.json", ".claude/settings.local.json",
                         "C:\\visual projects\\proj\\.claude\\settings.local.json"):
                reason = self.tool(name, path)
                self.assertIsNotNone(reason, f"{name} {path}")
                self.assertIn("Настройки Claude Code", reason)
        for cmd in ("echo '{}' > .claude/settings.json", "cp /c/x/s.json .claude/settings.local.json",
                    "rm .claude/settings.json", "mv .claude/settings.json data/old.json",
                    "cat x | tee .claude/settings.local.json"):
            self.assertIn("Настройки Claude Code", dg.check(cmd, CWD) or "", cmd)
        self.assertIsNone(self.tool("Write", "C:/visual projects/proj/.claude/roles/notes/engineer.md"))
        self.assertIsNone(dg.check("cat .claude/settings.json", CWD))

    # ------------------------------------------------------------------------------------------ (и) слово владельца
    def test_owner_record_closed_to_dispatcher_roles(self):
        for role in ("engineer", "researcher", "judge"):
            self.as_role(role)
            for name in ("Write", "Edit", "MultiEdit"):
                for path in ("C:/visual projects/proj/CLAUDE.md", "CLAUDE.md", ".claude/decisions.md",
                             "C:\\visual projects\\proj\\.claude\\goals\\G-001.md", "docs/CLAUDE.md"):
                    reason = self.tool(name, path)
                    self.assertIsNotNone(reason, f"{role} {name} {path}")
                    self.assertIn("Слово владельца", reason)
            for cmd in ("echo x >> CLAUDE.md", "echo '## В-9' >> .claude/decisions.md", "rm .claude/goals/G-001.md",
                        "cat x | tee -a .claude/decisions.md", "sed -i 's/a/b/' CLAUDE.md",
                        "Add-Content .claude\\decisions.md x", "mv .claude/goals/G-001.md /tmp/x"):
                reason = dg.check_tool({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": CWD})
                self.assertIn("Слово владельца", reason or "", f"{role}: {cmd}")
            for cmd in ("cat CLAUDE.md", "grep В-12 .claude/decisions.md", "ls .claude/goals",
                        'python registry.py show В-3', 'python tickets.py result TK-1 done --why "в CLAUDE.md > x"'):
                self.assertIsNone(dg.check_tool({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": CWD}), cmd)
            self.assertIsNone(self.tool("Write", "C:/visual projects/proj/.claude/roles/notes/engineer.md"))
            self.assertIsNone(self.tool("Write", "C:/visual projects/proj/docs/goals.md"))

    def test_owner_record_open_to_ceo(self):
        self.as_role(None)
        self.assertIsNone(self.tool("Edit", "C:/visual projects/proj/CLAUDE.md"))
        self.assertIsNone(self.tool("Write", "C:/visual projects/proj/.claude/decisions.md"))
        self.assertIsNone(dg.check_tool({"tool_name": "Bash", "tool_input": {"command": "echo x >> CLAUDE.md"},
                                         "cwd": CWD}))

    def test_errors_registry_closed_to_dispatcher_roles(self):
        """CP1: `.claude/errors/` пишет только скрипт errors.py; роль напрямую — ни файловым инструментом, ни оболочкой."""
        for role in ("engineer", "researcher", "judge"):
            self.as_role(role)
            for name in ("Write", "Edit", "MultiEdit"):
                for path in ("C:/visual projects/proj/.claude/errors/E-001.md", ".claude/errors/E-001.md",
                             "C:\\visual projects\\proj\\.claude\\errors\\E-012.md", "../proj/.claude/errors/E-003.md"):
                    reason = self.tool(name, path)
                    self.assertIsNotNone(reason, f"{role} {name} {path}")
                    self.assertIn("реестр ошибок", reason)
            for cmd in ("echo 'status: fixed' >> .claude/errors/E-001.md", "echo x > .claude/errors/E-001.md",
                        "rm .claude/errors/E-001.md", "rm -rf .claude/errors", "rm -rf ./.claude/errors/",
                        "sed -i 's/open/fixed/' .claude/errors/E-001.md", "cat x | tee .claude/errors/E-002.md",
                        "cp /tmp/E-001.md .claude/errors/E-001.md", "mv .claude/errors/E-001.md /tmp/x",
                        "Set-Content .claude\\errors\\E-001.md x", "python -c \"open('.claude/errors/E-001.md','w').write('x')\""):
                reason = dg.check_tool({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": CWD})
                self.assertIn("реестр ошибок", reason or "", f"{role}: {cmd}")
            self.assertIsNone(self.tool("Write", "C:/visual projects/proj/docs/errors/E-001.md"))      # не тот каталог
            self.assertIsNone(self.tool("Write", "C:/visual projects/proj/.claude/dispatcher/test_errors.py"))

    def test_errors_registry_reading_and_errors_py_stay_open_to_roles(self):
        """Читать реестр роли можно (в т. ч. с `2>&1`); сам errors.py пишет в него как раньше: страж смотрит на прямые записи
        инструментов и команд оболочки, а не на работу скрипта плагина."""
        script = "python C:/visual projects/plugin/.claude/dispatcher/errors.py"
        for role in ("engineer", "researcher", "judge"):
            self.as_role(role)
            for cmd in ("cat .claude/errors/E-001.md", "ls .claude/errors", "grep -l fixed .claude/errors/*.md",
                        "cat .claude/errors/E-001.md 2>/dev/null", "ls .claude/errors 2>&1 | head",
                        f'{script} add --title "цена > 0" --ticket TK-5 --what "см. E-001: open(x) вернул None"',
                        f"{script} fix E-001 --check \"python -m pytest tests/test_x.py\" --before abc1234 2>&1",
                        f"{script} list", f"{script} run --cwd .claude/worktrees/TK-5", f"{script} show E-001"):
                self.assertIsNone(dg.check_tool({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": CWD}), f"{role}: {cmd}")

    def test_errors_registry_open_to_ceo(self):
        self.as_role(None)
        self.assertIsNone(self.tool("Write", "C:/visual projects/proj/.claude/errors/E-001.md"))
        self.assertIsNone(dg.check_tool({"tool_name": "Bash", "tool_input": {"command": "echo x >> .claude/errors/E-001.md"},
                                         "cwd": CWD}))

    def test_settings_open_to_ceo(self):
        self.as_role(None)
        self.assertIsNone(self.tool("Edit", "C:/visual projects/proj/.claude/settings.json"))
        self.assertIsNone(dg.check("echo '{}' > .claude/settings.local.json", CWD))


class ProtectedPaths(unittest.TestCase):
    """CP7: «защищённые пути» — единственная настройка проекта вместо правил конкретного проекта в коде стража.
    `<проект>/.claude/rpv.json`, ключ `protected_paths` — список шаблонов; то, что под них попадает, не удаляет и не
    перезаписывает никто (роль и CEO одинаково). Нет настройки — защиты нет; настройка битая — отказ (fail-closed)."""

    FILES = ("data/raw/a.bin", "data/other/b.txt", "backups/old.tar", "notes.txt", "src/main.py", "keys/id.pem",
             "wt/README")

    def setUp(self):
        from unittest import mock
        self.d = tempfile.mkdtemp(prefix="dg-pp-", dir=str(Path.home()))    # под домашним: не временный каталог
        self.addCleanup(shutil.rmtree, self.d, True)
        self.base = Path(self.d).as_posix()
        saved = (dg.LOCAL_ROOTS, dg.WRITE_HOME_ROOTS, dg.PROJECT_DIR, dg.PROTECTED, dg.PROTECTED_ERROR, dg.REMOTE_ROOTS,
                 dg.STAGE, dg.CLOSED_RE)

        def restore():
            (dg.LOCAL_ROOTS, dg.WRITE_HOME_ROOTS, dg.PROJECT_DIR, dg.PROTECTED, dg.PROTECTED_ERROR, dg.REMOTE_ROOTS,
             dg.STAGE, dg.CLOSED_RE) = saved
        self.addCleanup(restore)
        for rel in self.FILES:
            f = Path(self.d, rel)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x", encoding="utf-8")
        p = mock.patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        self.as_role(None)
        dg.configure(self.d)
        dg.REMOTE_ROOTS, dg.STAGE, dg.CLOSED_RE, _ = dg.machine_rules(PROJ_RPV)  # машины проекта proj — как у остальных тестов

    def as_role(self, role):
        os.environ.pop("RPV_ROLE", None)
        if role:
            os.environ["RPV_ROLE"] = role

    def rpv(self, content):
        """Записать `.claude/rpv.json` проекта (объект → JSON вместе с машинами proj, строка — как есть) и перечитать."""
        import json
        path = Path(self.d, ".claude", "rpv.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps({**PROJ_RPV, **content}), encoding="utf-8")
        dg.configure(self.d)

    def check(self, cmd):
        return dg.check(cmd, self.d)

    def no(self, cmd, expect="rpv.json"):
        reason = self.check(cmd)
        self.assertIsNotNone(reason, f"пропущено: {cmd!r}")
        self.assertIn(expect, reason, cmd)

    def ok(self, cmd):
        reason = self.check(cmd)
        self.assertIsNone(reason, f"ложный отказ: {cmd!r} → {reason}")

    def tool(self, name, rel):
        key = "notebook_path" if name == "NotebookEdit" else "file_path"
        return dg.check_tool({"tool_name": name, "tool_input": {key: f"{self.base}/{rel}"}, "cwd": self.d})

    # ------------------------------------------------------------------------------------------ нет настройки
    def test_not_configured_means_no_protection(self):
        self.assertEqual((dg.PROTECTED, dg.PROTECTED_ERROR), ((), None))
        for content in ('{}', '{"other": 1}', '{"protected_paths": []}', '{"protected_paths": null}'):
            self.rpv(content)
            self.assertEqual((dg.PROTECTED, dg.PROTECTED_ERROR), ((), None), content)
            self.ok("rm -rf data/raw backups")
            self.ok("echo x > data/raw/a.bin")
            self.assertIsNone(self.tool("Write", "data/raw/a.bin"))

    def test_bom_unknown_keys_and_normalization(self):
        self.rpv('﻿{"protected_paths": ["Data\\\\Raw\\\\", "/Backups/", "*.PEM"], "later_setting": {"a": 1}}')
        self.assertEqual(dg.PROTECTED, ("data/raw", "/backups", "*.pem"))
        self.assertIsNone(dg.PROTECTED_ERROR)

    # ------------------------------------------------------------------------------------------ удаление и перезапись
    def test_delete_refused_for_ceo_and_every_role(self):
        self.rpv({"protected_paths": ["data/raw/**", "backups", "*.pem"]})
        for role in (None, "engineer", "researcher", "judge"):
            self.as_role(role)
            for cmd in ("rm -rf data/raw", "rm data/raw/a.bin", "rm -rf backups", "rm keys/id.pem", "find backups -delete",
                        "rm -rf data/raw/ data/other", "cd data && rm -rf raw", "rm -rf ./data/../data/raw",
                        "mv data/raw/a.bin trash.bin", "rsync -a --delete data/other/ data/raw/",
                        f'rm -rf "{self.base}/data/raw/a.bin"', "rm -rf DATA/RAW",
                        "python -c \"import shutil; shutil.rmtree('data/raw')\"", "bash -c 'rm -rf backups'",
                        "Remove-Item -Recurse backups"):
                self.no(cmd)
            for cmd in ("rm -rf data/other", "rm notes.txt", "rm -rf src", "rm keys/id.pem.bak", "ls data/raw", "cat backups/old.tar"):
                self.ok(cmd)
        reason = self.check("rm -rf data/raw")
        self.assertIn("data/raw/**", reason)                                      # какой шаблон сработал
        self.assertIn("ни роли, ни CEO", reason)

    def test_overwrite_refused_when_file_exists_new_file_is_creation(self):
        self.rpv({"protected_paths": ["data/raw/**", "backups"]})
        for role in (None, "engineer"):
            self.as_role(role)
            b = self.base
            for cmd in (f'echo x > "{b}/data/raw/a.bin"', f'cp notes.txt "{b}/data/raw/a.bin"',
                        f'truncate -s 0 "{b}/backups/old.tar"', f'echo x | tee "{b}/data/raw/a.bin"',
                        f'dd if=notes.txt of="{b}/data/raw/a.bin"', f'mv notes.txt "{b}/backups/old.tar"',
                        f"cat > \"{b}/backups/old.tar\" <<'EOF'\nx\nEOF"):
                self.no(cmd)
            for cmd in (f'echo x > "{b}/data/raw/new.bin"', f'cp notes.txt "{b}/data/raw/new.bin"',
                        f'echo x >> "{b}/data/raw/a.bin"', f'mv notes.txt "{b}/data/raw/new.bin"',
                        f'echo x > "{b}/data/other/b.txt"'):
                self.ok(cmd)
                self.assertFalse(Path(self.d, "data/raw/new.bin").exists())      # проверка ничего не выполняла

    def test_file_tools_follow_the_same_rule(self):
        self.rpv({"protected_paths": ["data/raw", "keys/id.pem"]})
        for role in (None, "engineer"):
            self.as_role(role)
            for name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                for rel in ("data/raw/a.bin", "keys/id.pem"):
                    reason = self.tool(name, rel)
                    self.assertIsNotNone(reason, f"{role} {name} {rel}")
                    self.assertIn("rpv.json", reason)
                self.assertIsNone(self.tool(name, "data/raw/new.bin"), f"{role} {name}")        # нового файла ещё нет
                self.assertIsNone(self.tool(name, "data/other/b.txt"), f"{role} {name}")

    # ------------------------------------------------------------------------------------------ как читаются шаблоны
    def test_name_pattern_matches_at_any_depth_and_does_not_block_parents(self):
        self.rpv({"protected_paths": ["raw", "*.pem"]})
        for cmd in ("rm -rf data/raw", "rm data/raw/a.bin", "rm -rf raw", "rm keys/id.pem", "rm -rf src/deep/raw/x",
                    "ssh srv@192.0.2.49 'rm -rf ~/proj/e1/raw/x'", "ssh srv@192.0.2.49 'rm -f ~/proj/keys/a.pem'"):
            self.no(cmd)
        for cmd in ("rm -rf data", "rm -rf data/rawer", "rm keys/id.pem.bak", "ssh srv@192.0.2.49 'rm -rf ~/proj/e1/tmp'"):
            self.ok(cmd)

    def test_anchored_pattern_is_from_project_root(self):
        self.rpv({"protected_paths": ["data/raw"]})
        for cmd in ("rm -rf data/raw", "rm data/raw/a.bin", "rm -rf data/ra*", "rm -rf data/*", "rm -rf data",
                    "rm -rf data/raw/../raw", f'rm -rf "{self.base}/data"', "ssh srv@192.0.2.49 'rm -rf ~/proj/x/data/raw'"):
            self.no(cmd)
        for cmd in ("rm -rf data/other", "rm -rf data/rawer", "rm -rf src/data/raw", "rm -rf src"):
            self.ok(cmd)

    def test_double_star(self):
        self.rpv({"protected_paths": ["data/**/secret", "**/*.key"]})
        for cmd in ("rm data/secret", "rm data/a/b/secret", "rm -rf data/a/b/secret/x", "rm keys/x.key", "rm x.key",
                    "rm -rf data/a"):                                  # внутри каталога под data/ может лежать secret
            self.no(cmd)
        for cmd in ("rm -rf src", "rm notes.txt", "rm keys/x.keys"):
            self.ok(cmd)

    def test_mask_in_target_may_hit_protected(self):
        self.rpv({"protected_paths": ["data/raw/keep.bin"]})
        self.no("rm data/raw/*.bin")
        self.no("rm data/raw/ke?p.bin")
        self.ok("rm data/raw/*.txt")
        self.ok("rm data/other/*.bin")

    def test_absolute_pattern(self):
        out = outside_dir("dg-vault-")
        self.addCleanup(shutil.rmtree, out, True)
        Path(out, "vault").mkdir()
        Path(out, "vault", "key").write_text("x", encoding="utf-8")
        Path(out, "free").mkdir()
        Path(out, "free", "f").write_text("x", encoding="utf-8")
        self.rpv({"protected_paths": [f"{self.base}/data/raw", f"{Path(out).as_posix()}/vault"]})
        self.no(f'rm -rf "{self.base}/data/raw/a.bin"')
        self.no("rm -rf data/raw")                                                  # тот же путь относительным
        self.ok(f'rm -rf "{self.base}/data/other"')
        self.ok("ssh srv@192.0.2.49 'rm -rf ~/proj/data/raw'")                       # другой компьютер — другой путь
        for name in ("Write", "Edit"):                                              # CEO правит вне проекта, но не здесь
            key = "file_path"
            ev = lambda rel: {"tool_name": name, "tool_input": {key: f"{Path(out).as_posix()}/{rel}"}, "cwd": self.d}
            self.assertIsNotNone(dg.check_tool(ev("vault/key")), name)
            self.assertIsNone(dg.check_tool(ev("free/f")), name)

    def test_absolute_pattern_on_remote_machine(self):
        self.rpv({"protected_paths": ["/opt/proj-compute/keep"]})
        self.no("ssh root@203.0.113.2 'rm -rf /opt/proj-compute/keep/x'")
        self.ok("ssh root@203.0.113.2 'rm -rf /opt/proj-compute/other'")

    def test_home_directory_is_not_part_of_names(self):
        from unittest import mock
        self.rpv({"protected_paths": ["root"]})
        with mock.patch.object(dg.os.path, "expanduser", return_value="/root"):
            ctx = dg.Ctx(None)
            self.assertIsNone(dg.protected_match("/root/.claude/settings.json", ctx, False))
            self.assertEqual(dg.protected_match("/root/data/root/x.bin", ctx, False), "root")

    def test_temp_and_scratch_do_not_override_protection(self):
        self.rpv({"protected_paths": ["secret"]})
        self.no("rm -rf /tmp/x/secret")
        self.no("rm -rf $TMP/secret/a")
        self.ok("rm -rf /tmp/x/open")

    def test_git_reset_hard_is_refused_inside_protected_worktree(self):
        Path(self.d, "wt", ".git").write_text("gitdir: ../.git/worktrees/wt", encoding="utf-8")
        self.ok(f'cd "{self.base}/wt" && git reset --hard X')                       # связанный worktree — можно
        self.rpv({"protected_paths": ["wt"]})
        self.no(f'cd "{self.base}/wt" && git reset --hard X', expect="через CEO")

    # ------------------------------------------------------------------------------------------ битая настройка
    BROKEN = ("{", "", "[]", '"data"', '{"protected_paths": "data/raw"}', '{"protected_paths": {"a": 1}}',
              '{"protected_paths": [1]}', '{"protected_paths": [""]}', '{"protected_paths": ["  /  "]}',
              '{"protected_paths": ["../outside"]}', '{"protected_paths": ["a/../b"]}', '{"protected_paths": ["x", null]}')

    def test_malformed_config_is_fail_closed_with_clear_reason(self):
        for content in self.BROKEN:
            self.rpv(content)
            self.assertTrue(dg.PROTECTED_ERROR, content)
            self.assertEqual(dg.PROTECTED, (), content)
            for role in (None, "engineer"):
                self.as_role(role)
                for cmd in ("rm -rf data/other", "rm notes.txt", f'echo x > "{self.base}/notes.txt"',
                            f'cp src/main.py "{self.base}/notes.txt"', "find backups -delete", "mv notes.txt old.txt"):
                    reason = self.check(cmd)
                    self.assertIsNotNone(reason, f"{content!r}: {cmd}")
                    self.assertIn(".claude/rpv.json", reason)
                    self.assertIn("protected_paths", reason)
                    self.assertIn("fail-closed", reason)
                self.assertIsNotNone(self.tool("Write", "notes.txt"), content)
                self.assertIsNotNone(self.tool("Edit", "src/main.py"), content)
                for cmd in ("ls", "cat notes.txt", "grep -r x src", "echo x > fresh.txt", "git status", "mkdir newdir"):
                    self.ok(cmd)                                                    # читать и создавать новое — можно
                self.assertIsNone(self.tool("Write", "fresh.txt"))

    def test_unreadable_config_is_fail_closed(self):
        Path(self.d, ".claude", "rpv.json").mkdir(parents=True)                      # вместо файла — каталог
        dg.configure(self.d)
        self.assertIn("не прочитан", dg.PROTECTED_ERROR)
        self.no("rm -rf data/other", expect="fail-closed")

    def test_broken_config_can_be_fixed_by_ceo_only(self):
        self.rpv("{")
        self.assertIsNone(self.tool("Write", ".claude/rpv.json"))                    # CEO правит файл настроек
        self.assertIsNone(self.tool("Edit", ".claude/rpv.json"))
        self.ok("echo '{}' > .claude/rpv.json")
        self.ok("rm .claude/rpv.json")
        self.as_role("engineer")                                                    # роль — нет: настройки плагина закрыты
        self.assertIn("Настройки Claude Code и плагина", self.tool("Write", ".claude/rpv.json"))
        self.no("echo '{}' > .claude/rpv.json", expect="Настройки Claude Code и плагина")
        self.no("rm .claude/rpv.json", expect="Настройки Claude Code и плагина")
        self.rpv({"protected_paths": ["data/raw"]})                                  # исправлено — защита снова работает
        self.ok("rm -rf data/other")
        self.no("rm -rf data/raw")

    def test_role_cannot_lift_protection_by_editing_the_config(self):
        self.rpv({"protected_paths": ["data/raw"]})
        self.as_role("engineer")
        for name in ("Write", "Edit", "MultiEdit"):
            self.assertIn("Настройки Claude Code и плагина", self.tool(name, ".claude/rpv.json"), name)
        self.no("cp notes.txt .claude/rpv.json", expect="Настройки Claude Code и плагина")
        self.as_role(None)                                                          # CEO — владелец настройки
        self.assertIsNone(self.tool("Edit", ".claude/rpv.json"))

    def test_load_protected_directly(self):
        self.assertEqual(dg.load_protected(self.d), ((), None))                    # файла нет
        self.rpv({"protected_paths": ["a", "b/c"]})
        self.assertEqual(dg.load_protected(self.d), (("a", "b/c"), None))
        out, err = dg.load_protected(Path(self.d, "nope"))
        self.assertEqual((out, err), ((), None))


class ProtectedPathsEntryPoint(unittest.TestCase):
    """Те же правила через точку входа хука (процесс `delete_guard.py`, проект со своей `.claude/rpv.json`)."""

    HERE = os.path.dirname(os.path.abspath(__file__))

    def run_hook(self, project, event, role=None):
        import json
        import subprocess
        env = dict(os.environ, CLAUDE_PROJECT_DIR=project)
        env.pop("RPV_ROLE", None)
        if role:
            env["RPV_ROLE"] = role
        r = subprocess.run([sys.executable, os.path.join(self.HERE, "delete_guard.py")],
                           input=json.dumps(event).encode("utf-8"), capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.decode("utf-8").strip()
        return json.loads(out)["hookSpecificOutput"] if out else None

    def test_protected_paths_through_entry_point(self):
        with tempfile.TemporaryDirectory(prefix="dg-ep-", dir=str(Path.home())) as d:
            base = Path(d)
            (base / ".claude" / "roles").mkdir(parents=True)
            (base / ".claude" / "dispatcher").mkdir()                              # сюда страж пишет журнал
            (base / "data" / "raw").mkdir(parents=True)
            (base / "data" / "raw" / "a.bin").write_text("x", encoding="utf-8")
            rm = {"tool_name": "Bash", "tool_input": {"command": "rm -rf data/raw"}, "cwd": d}
            wr = {"tool_name": "Write", "tool_input": {"file_path": (base / "data" / "raw" / "a.bin").as_posix()}, "cwd": d}
            new = {"tool_name": "Write", "tool_input": {"file_path": (base / "data" / "raw" / "n.bin").as_posix()}, "cwd": d}
            self.assertIsNone(self.run_hook(d, rm))                                  # настройки нет — удалять можно
            (base / ".claude" / "rpv.json").write_text('{"protected_paths": ["data/raw/**"]}', encoding="utf-8")
            for role in (None, "engineer", "judge"):
                for ev in (rm, wr):
                    out = self.run_hook(d, ev, role)
                    self.assertEqual(out["permissionDecision"], "deny", (role, ev["tool_name"]))
                    self.assertIn("Защищённый путь", out["permissionDecisionReason"])
                self.assertIsNone(self.run_hook(d, new, role))
            log = (base / ".claude" / "dispatcher" / "guard.log").read_text(encoding="utf-8")
            self.assertIn("\tprotected\tdeny\t", log)
            (base / ".claude" / "rpv.json").write_text('{"protected_paths": "data/raw"}', encoding="utf-8")
            out = self.run_hook(d, rm)
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertIn("не прочитаны", out["permissionDecisionReason"])
            self.assertIn("fail-closed", out["permissionDecisionReason"])
            self.assertIsNone(self.run_hook(d, dict(rm, tool_input={"command": "ls data"})))
            (base / ".claude" / "rpv.json").unlink()
            self.assertIsNone(self.run_hook(d, rm))                                  # файл убрали — защиты нет


class Publication(unittest.TestCase):
    """CP2 (критерий 14): публикации — выпуск, слияние PR, пуш в main/master и теги, пакеты и образы — запуску диспетчера только с
    `allow: publish (В-N)` в шапке его тикета (`tickets.py allow`, ставит CEO); сессии CEO и владельца не ограничены. Шапка не
    прочитана — отказ. Запись строки `allow:` в файл тикета запуском диспетчера — отказ."""

    TID = "TK-7"
    ALLOW = "allow: publish (В-3)"
    DENY = (
        "gh release create v1.2.3 --notes x", "gh release upload v1.2.3 dist.zip", "gh release edit v1.2.3 --draft=false",
        "gh pr merge 5 --squash", "gh -R o/r pr merge 5", "gh.exe release create v1",
        "git push origin main", "git push origin master", "git push -u origin main", "git push origin HEAD:main",
        "git push origin HEAD:refs/heads/main", "git push origin feat/x:main", "git push upstream refs/heads/main",
        "git push --tags", "git push origin --tags", "git push --follow-tags origin feat/x", "git push --all",
        "git push origin v1.8.48", "git push origin refs/tags/v1.8.48", "git push origin tag v1.8.48",
        "git push --repo=origin main", "git -C data push origin main", "git -c core.x=y push origin main",
        "npm publish", "npm --otp 123456 publish", "pnpm publish --no-git-checks", "yarn publish", "yarn npm publish",
        "cargo publish", "poetry publish --build", "uv publish", "uv run twine upload dist/*", "twine upload dist/*",
        "python -m twine upload dist/*", "py -3 -m twine upload dist/*", "docker push reg/img:1", "docker image push reg/img:1",
        "gem push x.gem",
        "sudo gh pr merge 5", "bash -c 'git push origin main'", "echo ok && git push origin main",
        "ssh srv@192.0.2.49 'cd ~/proj && git push origin main'", 'ssh srv@192.0.2.49 "gh release create v1"',
        "ssh srv@192.0.2.49 'cd ~/proj && git push'",              # на удалённой машине текущая ветка неизвестна
        "python -c \"import subprocess; subprocess.run(['git', 'push', 'origin', 'main'])\"",
        "echo 'abc; git push origin main",                           # не разобрать — прежний регэксп как страховка
    )
    DENY_PS = (
        "git push origin main", "& gh release create v1 --notes x", 'powershell -Command "git push origin HEAD:main"',
        'pwsh -c "npm publish"', "Set-Location C:\\visual projects\\proj; gh pr merge 5",
        '& "C:\\Program Files\\Git\\bin\\git.exe" push origin main', "if ($?) { git push origin main }",
    )
    # слияние и выпуск мимо `gh pr merge`/`gh release`: `gh api` с записью и curl/wget/Invoke-WebRequest на api.github.com
    DENY_API = (
        "gh api -X PUT repos/o/r/pulls/5/merge", "gh api --method PUT /repos/o/r/pulls/5/merge -f merge_method=squash",
        "gh api repos/o/r/pulls/5/merge -X PUT", "gh api repos/o/r/pulls/5/merge -f merge_method=squash",   # поле без -X — POST
        "gh api repos/o/r/pulls/5/merge -F sha=abc", "gh api -XPUT repos/o/r/pulls/5/merge", "gh api --method=PUT repos/o/r/pulls/5/merge",
        "gh api --method put repos/o/r/pulls/5/merge", "gh -R o/r api -X PUT repos/o/r/pulls/5/merge",
        "gh.exe api -X PUT repos/{owner}/{repo}/pulls/5/merge", "sudo gh api -X PUT repos/o/r/pulls/5/merge",
        "gh api -X POST repos/o/r/merges -f base=main -f head=feat", "gh api -X POST repos/o/r/releases -f tag_name=v1",
        "gh api repos/o/r/releases -f tag_name=v1", "gh api -X PATCH repos/o/r/releases/12 -F draft=false",
        "gh api --method DELETE repos/o/r/releases/12", "gh api https://api.github.com/repos/o/r/releases -f tag_name=v1",
        "gh api -X POST repos/o/r/git/refs -f ref=refs/heads/x -f sha=abc", "gh api -X PATCH repos/o/r/git/refs/heads/main -f sha=abc",
        "gh api -X POST repos/o/r/git/tags -f tag=v1", "gh api -X PUT repos/o/r/contents/README.md -f message=m -f content=Zm9v",
        "gh api repos/o/r/contents/README.md --input body.json",
        "gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: \"x\"}) { clientMutationId } }'",
        "curl -X PUT https://api.github.com/repos/o/r/pulls/5/merge", "curl -XPUT https://api.github.com/repos/o/r/pulls/5/merge",
        "curl -sSL -X POST -H 'Authorization: token x' https://api.github.com/repos/o/r/releases -d '{\"tag_name\":\"v1\"}'",
        "curl -d '{}' https://api.github.com/repos/o/r/releases", "curl -sd '{}' https://api.github.com/repos/o/r/releases",
        "curl --data-binary @a.json https://api.github.com/repos/o/r/releases", "curl --data-raw '{}' https://api.github.com/x",
        "curl --json '{}' https://api.github.com/repos/o/r/releases",
        "curl -F file=@a.zip https://uploads.github.com/repos/o/r/releases/1/assets?name=a.zip",
        "curl -T a.zip https://uploads.github.com/repos/o/r/releases/1/assets?name=a.zip",
        "curl --upload-file a.zip https://uploads.github.com/repos/o/r/releases/1/assets",
        "curl --request DELETE https://api.github.com/repos/o/r/releases/1", "curl --request=PATCH https://api.github.com/repos/o/r/releases/1",
        "curl https://api.github.com/repos/o/r/releases -X POST", "curl.exe -X POST https://api.github.com/x",
        "curl -X post -H 'A: b' 'https://api.github.com/x'", "echo ok && curl -X POST https://api.github.com/x",
        "wget --method=PUT https://api.github.com/repos/o/r/pulls/5/merge", "wget --method PUT https://api.github.com/x",
        "wget --post-data='{}' https://api.github.com/repos/o/r/releases", "wget --post-file=a.json https://api.github.com/x",
        "wget --body-data='{}' --method=PATCH https://api.github.com/x",
        "bash -c 'gh api -X PUT repos/o/r/pulls/5/merge'", "ssh srv@192.0.2.49 'curl -X POST https://api.github.com/x'",
        "echo 'abc; gh api -X PUT repos/o/r/pulls/5/merge",          # не разобрать — прежний регэксп как страховка
        "echo 'abc; curl -X POST https://api.github.com/x",
    )
    DENY_API_PS = (
        "Invoke-WebRequest -Method Put -Uri https://api.github.com/repos/o/r/pulls/5/merge",
        "Invoke-RestMethod -Uri https://api.github.com/repos/o/r/releases -Method Post -Body $b",
        "Invoke-RestMethod -Method:Post https://api.github.com/repos/o/r/releases", "iwr -Method Delete https://api.github.com/x",
        "irm -Method PATCH -Uri https://api.github.com/repos/o/r/releases/1", "Invoke-RestMethod https://api.github.com/x -Meth Post",
        "Invoke-WebRequest -Uri https://uploads.github.com/repos/o/r/releases/1/assets -Method Post -InFile a.zip",
        "Invoke-RestMethod https://api.github.com/repos/o/r/releases -Body $b",        # тело без -Method — POST
        "curl -Method Post -Uri https://api.github.com/x", "curl.exe -X POST https://api.github.com/x",
        "$r = Invoke-RestMethod -Method Put -Uri 'https://api.github.com/repos/o/r/pulls/5/merge'",
        'powershell -Command "Invoke-RestMethod -Method Put -Uri https://api.github.com/x"',
        "& gh api -X PUT repos/o/r/pulls/5/merge",
    )
    ALLOW_API = (        # чтение, запись не на GitHub и запись GitHub, не похожая на слияние и выпуск (gh api)
        "gh api repos/o/r/pulls/5", "gh api repos/o/r/releases", "gh api repos/o/r/pulls/5/merge", "gh api user --jq .login",
        "gh api repos/o/r/commits --paginate", "gh api repos/o/r/pulls/5/merge -X GET",
        "gh api repos/o/r/releases -X GET -f per_page=5", "gh api --method GET repos/o/r/contents/README.md -f ref=main",
        "gh api -X POST repos/o/r/issues/5/comments -f body=x", "gh api -X PATCH repos/o/r/pulls/5 -f state=closed",
        "gh api graphql -f query='query { viewer { login } }'",
        "curl https://api.github.com/repos/o/r/releases", "curl -s -H 'Accept: x' https://api.github.com/repos/o/r/pulls/5",
        "curl -I https://api.github.com/x", "curl -X GET https://api.github.com/x",
        "curl -G -d per_page=5 https://api.github.com/repos/o/r/releases",         # -G: данные уходят в адрес, это GET
        "curl -X POST https://example.com/hook -d x", "curl -d x=y https://example.com/hook",
        "curl -X POST https://example.com/api.github.com.txt", "curl -o a.zip -L https://github.com/o/r/archive/main.zip",
        "wget https://api.github.com/repos/o/r/releases", "wget -q -O - https://api.github.com/x",
        "wget --post-data=x https://example.com", "wget --method=GET https://api.github.com/x",
        "Invoke-WebRequest -Uri https://api.github.com/repos/o/r/releases", "Invoke-RestMethod https://api.github.com/x",
        "Invoke-RestMethod -Method Get -Uri https://api.github.com/x", "iwr https://api.github.com/x",
        "Invoke-RestMethod -Method Post -Uri https://example.com/hook -Body $b", "curl https://api.github.com/x",
        'git commit -m "curl -X POST https://api.github.com/x"', 'echo "gh api -X PUT repos/o/r/pulls/5/merge"',
        "grep -n 'curl -X POST api.github.com' README.md",
        'python .claude/dispatcher/tickets.py comment TK-7 --author engineer --text "curl -X POST https://api.github.com/x"',
    )
    ALLOW_OK = (
        "git push -u origin feat/cp2-publish", "git push origin HEAD:feat/x", "git push origin feat/x:feat/y",
        "git push origin v2", "git push origin release/1.2", "git push -o ci.skip origin feat/x",
        "git push --dry-run origin main", "git push -n origin master",
        'git commit -m "git push origin main"', 'echo "gh release create v1"', "grep -n 'npm publish' README.md",
        'python .claude/dispatcher/tickets.py comment TK-7 --author engineer --text "git push origin main и gh pr merge 5"',
        'python .claude/dispatcher/tickets.py result TK-7 blocked --why "нужен gh release create"',
        "gh pr create --title x", "gh pr view 5", "gh pr checks 5", "gh release list", "gh release view v1",
        "npm install", "npm run build", "npm test", "cargo build", "docker build .", "docker run img", "twine check dist/*",
        "uv run pytest", "python -m pytest -q", "git status", "git log --oneline",
    )

    def setUp(self):
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory(prefix="dg-pub-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ticket = self.root / ".claude" / "tickets" / f"{self.TID}.md"
        self.ticket.parent.mkdir(parents=True)
        self.write_ticket()
        for p in (mock.patch.dict(os.environ, {"RPV_ROLE": "engineer", "RPV_TICKET": self.TID}),
                  mock.patch.object(dg, "PROJECT_DIR", str(self.root))):
            p.start()
            self.addCleanup(p.stop)

    def write_ticket(self, allow=None, body="## Лог\n### 2026-10-13T10:00:00+04:00 ceo\nтекст\n"):
        extra = [] if allow is None else [allow]
        head = ["id: " + self.TID, "title: Выпуск", "owner: engineer", "status: in_progress", *extra, "updated: 2026-10-13T10:00:00+04:00"]
        self.ticket.write_text("---\n" + "\n".join(head) + "\n---\n\n" + body, encoding="utf-8")

    def as_ceo(self):
        os.environ.pop("RPV_ROLE", None)

    def reason(self, cmd, tool="Bash", cwd=CWD):
        return dg.check_tool({"tool_name": tool, "tool_input": {"command": cmd}, "cwd": cwd})

    def all_denied(self):
        for tool, cmds in (("Bash", self.DENY + self.DENY_API), ("PowerShell", self.DENY_PS + self.DENY_API_PS)):
            for cmd in cmds:
                reason = self.reason(cmd, tool)
                self.assertIsNotNone(reason, f"пропущено: {tool} {cmd!r}")
                self.assertIn("Публикация", reason, f"{cmd!r}")

    def all_allowed(self):
        for tool, cmds in (("Bash", self.DENY + self.DENY_API), ("PowerShell", self.DENY_PS + self.DENY_API_PS)):
            for cmd in cmds:
                self.assertIsNone(self.reason(cmd, tool), f"ложный отказ: {tool} {cmd!r}")

    # ------------------------------------------------------------------------------------------ роль без согласия
    def test_publication_denied_to_role_without_consent(self):
        for role in ("engineer", "researcher", "judge"):
            os.environ["RPV_ROLE"] = role
            self.all_denied()

    def test_reason_says_how_to_get_consent(self):
        reason = self.reason("git push origin main")
        for part in ("TK-7", "tickets.py allow <ID> publish --ref В-N", "ask-owner", "--why-owner необратимое", "registry.py decide",
                     "merge_rule", "git push origin <ветка>"):
            self.assertIn(part, reason)

    def test_working_branch_push_and_plain_commands_allowed(self):
        for cmd in self.ALLOW_OK:
            self.assertIsNone(self.reason(cmd), f"ложный отказ: {cmd!r}")

    def test_api_reads_and_other_writes_allowed(self):
        """gh api без записи, curl GET, запись на чужой адрес и запись GitHub мимо слияния и выпуска (комментарий) — свободны."""
        for role in ("engineer", "researcher", "judge"):
            os.environ["RPV_ROLE"] = role
            for tool in ("Bash", "PowerShell"):
                for cmd in self.ALLOW_API:
                    self.assertIsNone(self.reason(cmd, tool), f"ложный отказ: {role} {tool} {cmd!r}")

    def test_api_write_reason_is_the_publication_text(self):
        for cmd in ("gh api -X PUT repos/o/r/pulls/5/merge", "curl -X POST https://api.github.com/repos/o/r/releases -d '{}'"):
            reason = self.reason(cmd)
            self.assertIn("Публикация", reason, cmd)
            for part in ("TK-7", "tickets.py allow <ID> publish --ref В-N", "ask-owner", "--why-owner необратимое"):
                self.assertIn(part, reason, cmd)
        self.assertIn("gh api PUT repos/o/r/pulls/5/merge", self.reason("gh api -X PUT repos/o/r/pulls/5/merge"))
        self.assertIn("curl: запись на api.github.com", self.reason("curl -X POST https://api.github.com/x"))

    def test_api_write_journal_group(self):
        tr = []
        self.assertIsNotNone(dg.check_tool({"tool_name": "Bash", "tool_input": {"command": "gh api -X PUT repos/o/r/pulls/5/merge"}, "cwd": CWD}, tr))
        self.assertEqual(tr[-1], ("publish", "deny"))

    def test_force_push_stays_irreversible_refusal(self):
        for cmd in ("git push --force origin feat/x", "git push -f origin main", "git push origin +feat/x"):
            self.assertIn("Необратимая команда git", self.reason(cmd), cmd)

    def test_ticket_text_does_not_trigger(self):
        for text in ("git push origin main", "gh pr merge 5", "npm publish", "docker push img"):
            self.assertIsNone(self.reason(f'python tickets.py comment TK-7 --author engineer --text "{text}"'), text)
            self.assertIsNone(self.reason(f"python tickets.py result TK-7 blocked --why '{text}'"), text)
        self.assertIsNone(self.reason('python tickets.py result TK-7 ask-owner --why "git push origin main нужен" --why-owner необратимое'))

    def test_journal_group(self):
        tr = []
        self.assertIsNotNone(dg.check_tool({"tool_name": "Bash", "tool_input": {"command": "npm publish"}, "cwd": CWD}, tr))
        self.assertEqual(tr[-1], ("publish", "deny"))

    # ------------------------------------------------------------------------------------------ сессия CEO / владельца
    def test_ceo_and_owner_sessions_unrestricted(self):
        self.as_ceo()
        self.all_allowed()
        os.environ["RPV_ROLE"] = "ceo"                                       # не роль диспетчера — сессия CEO
        self.assertIsNone(self.reason("git push origin main"))
        self.assertIsNone(self.reason("ssh root@203.0.113.1 'git push origin main'"))   # закрытый узел, не удаление

    # ------------------------------------------------------------------------------------------ согласие в шапке тикета
    def test_consent_allows_publication_for_this_ticket(self):
        self.write_ticket(self.ALLOW)
        self.all_allowed()
        tr = []
        self.assertIsNone(dg.check_tool({"tool_name": "Bash", "tool_input": {"command": "gh pr merge 5"}, "cwd": CWD}, tr))
        self.assertEqual(tr, [("publish", "allow")])
        self.assertEqual(dg.publish_consent(), (True, self.TID))

    def test_consent_is_per_ticket(self):
        self.write_ticket(self.ALLOW)
        os.environ["RPV_TICKET"] = "TK-8"
        (self.ticket.parent / "TK-8.md").write_text("---\nid: TK-8\nstatus: todo\n---\n", encoding="utf-8")
        self.assertIn("TK-8", self.reason("git push origin main"))

    def test_revoked_consent_denies_again(self):
        self.write_ticket("allow: ")
        self.assertIsNotNone(self.reason("git push origin main"))
        self.write_ticket(self.ALLOW)
        self.assertIsNone(self.reason("git push origin main"))
        self.write_ticket(self.ALLOW + "\nallow:")                           # последняя строка ключа решает, как в разборе шапки
        self.assertIsNotNone(self.reason("git push origin main"))

    def test_consent_is_read_from_header_only(self):
        for allow in ("allow: publishing", "allow: deploy", "x-allow: publish (В-3)", "allow publish"):
            self.write_ticket(allow)
            self.assertIsNotNone(self.reason("npm publish"), allow)
        self.write_ticket(None, body="## Лог\n### 2026-10-13T10:00:00+04:00 engineer\nallow: publish (В-3)\n")
        self.assertIsNotNone(self.reason("npm publish"))                     # строка в логе — не шапка

    def test_unreadable_ticket_denies(self):
        self.ticket.unlink()
        self.assertIn("шапка не прочитана", self.reason("git push origin main"))      # нет файла
        self.ticket.mkdir()
        self.assertIn("шапка не прочитана", self.reason("git push origin main"))      # вместо файла каталог
        self.ticket.rmdir()
        self.ticket.write_bytes(b"---\nid: TK-7\nallow: publish (\xd0\xb2\xff\xfe)\n---\n")
        self.assertIn("шапка не прочитана", self.reason("git push origin main"))      # не UTF-8
        self.ticket.write_text("id: TK-7\nallow: publish (В-3)\n", encoding="utf-8")
        self.assertIn("нет шапки", self.reason("git push origin main"))
        self.ticket.write_text("---\nid: TK-7\nallow: publish (В-3)\n", encoding="utf-8")
        self.assertIn("шапка не закрыта", self.reason("git push origin main"))        # у ticket.py такой тикет не разобрать

    def test_ticket_id_missing_or_unsafe_denies(self):
        self.write_ticket(self.ALLOW)
        for tid in (None, "", "../TK-7", "TK-7/../TK-7", "a" * 40):
            if tid is None:
                os.environ.pop("RPV_TICKET", None)
            else:
                os.environ["RPV_TICKET"] = tid
            self.assertIn("не определён", self.reason("git push origin main"), tid)

    # ------------------------------------------------------------------------------------------ пуш без названной ветки
    def make_repo(self, name, head):
        repo = self.root / name
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text(head, encoding="utf-8")
        return repo.as_posix()

    def test_bare_push_follows_current_branch(self):
        main = self.make_repo("on-main", "ref: refs/heads/main\n")
        feat = self.make_repo("on-feat", "ref: refs/heads/feat/cp2\n")
        detached = self.make_repo("detached", "0123456789abcdef0123456789abcdef01234567\n")
        for cmd in ("git push", "git push origin", "git push origin HEAD", "git push -u origin HEAD", "git push origin @"):
            self.assertIn("текущая — main", self.reason(cmd, cwd=main), cmd)
            self.assertIsNone(self.reason(cmd, cwd=feat), cmd)
            self.assertIn("не определить", self.reason(cmd, cwd=detached), cmd)
        self.assertIsNone(self.reason("git push origin feat/x", cwd=main))             # ветка названа — текущая не при чём
        self.assertIn("не определить", self.reason("git push origin HEAD", cwd=None))
        self.assertIn("текущая — main", self.reason(f"git -C {main} push", cwd=feat))
        self.assertIsNone(self.reason(f"git -C {feat} push", cwd=main))
        self.assertIsNone(self.reason(f"cd {feat} && git push", cwd=main))
        self.assertIn("не определить", self.reason("cd $WT && git push", cwd=feat))    # переменная в cd: каталог неизвестен

    def test_bare_push_in_linked_worktree_reads_its_head(self):
        main = self.make_repo("main-tree", "ref: refs/heads/main\n")
        gitdir = self.root / "main-tree" / ".git" / "worktrees" / "tk7"
        gitdir.mkdir(parents=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/feat/tk7\n", encoding="utf-8")
        wt = self.root / "main-tree" / ".claude" / "worktrees" / "tk7"
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {gitdir.as_posix()}\n", encoding="utf-8")
        self.assertIsNone(self.reason("git push -u origin HEAD", cwd=wt.as_posix()))
        self.assertIn("текущая — main", self.reason("git push -u origin HEAD", cwd=main))
        (wt / ".git").write_text("gitdir: ../../../.git/worktrees/tk7\n", encoding="utf-8")   # относительный gitdir
        self.assertIsNone(self.reason("git push", cwd=wt.as_posix()))
        (wt / ".git").write_text("мусор\n", encoding="utf-8")
        self.assertIn("не определить", self.reason("git push", cwd=wt.as_posix()))

    def test_bare_push_is_free_for_ceo(self):
        self.as_ceo()
        self.assertIsNone(self.reason("git push origin HEAD", cwd=self.make_repo("on-main", "ref: refs/heads/main\n")))
        self.assertIsNone(self.reason("git push", cwd=None))

    # ------------------------------------------------------------------------------------------ подделка согласия
    def edit(self, tool, path, text):
        if tool == "MultiEdit":
            ti = {"file_path": path, "edits": [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": text}]}
        else:
            ti = {"file_path": path, **({"content": text} if tool == "Write" else {"old_string": "status:", "new_string": text})}
        return dg.check_tool({"tool_name": tool, "tool_input": ti, "cwd": CWD})

    def test_role_cannot_write_allow_into_ticket(self):
        for tool in ("Write", "Edit", "MultiEdit"):
            for path in (self.ticket.as_posix(), "C:\\visual projects\\proj\\.claude\\tickets\\TK-9.md", ".claude/tickets/TK-7.md"):
                for text in ("allow: publish (В-3)\nstatus:", "id: x\n  Allow : publish\n"):
                    self.assertEqual(self.edit(tool, path, text), dg.REASON_CONSENT, f"{tool} {path} {text!r}")
        for cmd in ('echo "allow: publish (В-3)" >> .claude/tickets/TK-7.md',
                    "sed -i 's/^status:.*/allow: publish (В-3)/' .claude/tickets/TK-7.md",
                    "cat >> .claude/tickets/TK-7.md <<'EOF'\nallow: publish (В-3)\nEOF",
                    "python -c \"open('.claude/tickets/TK-7.md', 'a').write('allow: publish')\"",
                    "tee -a .claude/tickets/TK-7.md <<< 'allow: publish'"):
            self.assertEqual(self.reason(cmd), dg.REASON_CONSENT, cmd)
        self.assertEqual(self.reason('Add-Content .claude\\tickets\\TK-7.md "allow: publish (В-3)"', "PowerShell"), dg.REASON_CONSENT)

    def test_ordinary_ticket_work_is_not_forgery(self):
        for tool in ("Write", "Edit", "MultiEdit"):
            self.assertIsNone(self.edit(tool, self.ticket.as_posix(), "status: in_review\n"), tool)
            self.assertIsNone(self.edit(tool, (self.root / "docs" / "x.md").as_posix(), "allow: publish\n"), tool)   # не тикет
        for cmd in ("cat .claude/tickets/TK-7.md", "grep '^allow:' .claude/tickets/TK-7.md 2>/dev/null",
                    "grep -n allow .claude/tickets/TK-7.md 2>&1", "echo 'allow: publish' > notes.txt",
                    'python tickets.py comment TK-7 --author engineer --text "строку allow: пишет CEO в .claude/tickets/"',
                    "sed -i 's/in_progress/in_review/' .claude/tickets/TK-7.md"):
            self.assertIsNone(self.reason(cmd), cmd)

    def test_ceo_may_edit_tickets(self):
        self.as_ceo()
        self.assertIsNone(self.edit("Edit", self.ticket.as_posix(), "allow: publish (В-3)\nstatus:"))
        self.assertIsNone(self.reason('echo "allow: publish (В-3)" >> .claude/tickets/TK-7.md'))


class HookEntryPoint(unittest.TestCase):
    """Точка входа PreToolUse: JSON со stdin → deny с причиной; не наш инструмент / нет `.claude/roles` в проекте — тишина;
    битое событие и сбой стража — отказ (fail-closed); обёртка в hooks.json — отказ, когда нет Python или страж сломан."""

    HERE = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.dirname(os.path.dirname(HERE))

    def run_hook(self, event, project, with_roles=True, raw=None, script=None, role=None, ticket=None):
        import json
        import subprocess
        if with_roles:
            os.makedirs(os.path.join(project, ".claude", "roles"), exist_ok=True)
        env = dict(os.environ, CLAUDE_PROJECT_DIR=project)
        env.pop("RPV_ROLE", None)
        env.pop("ALPHA_ROLE", None)
        if role:
            env["RPV_ROLE"] = role
        if ticket:
            env["RPV_TICKET"] = ticket
        r = subprocess.run([sys.executable, script or os.path.join(self.HERE, "delete_guard.py")],
                           input=(raw if raw is not None else json.dumps(event)).encode("utf-8"),
                           capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.decode("utf-8").strip()

    def decision(self, out):
        import json
        return json.loads(out)["hookSpecificOutput"] if out else None

    def test_deny_allow_and_silent_cases(self):
        with tempfile.TemporaryDirectory() as d:
            ev = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /etc/x"}, "cwd": d}
            out = self.decision(self.run_hook(ev, d))
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertIn("Удаление запрещено", out["permissionDecisionReason"])
            ok = dict(ev, tool_input={"command": "rm -rf " + d.replace("\\", "/") + "/data/tmp"})
            self.assertEqual(self.run_hook(ok, d), "")                       # внутри проекта — можно
            self.assertEqual(self.run_hook(dict(ev, tool_name="Read"), d), "")
        with tempfile.TemporaryDirectory() as d2:                           # проект без команды ролей — молчит
            ev = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /etc/x"}, "cwd": d2}
            self.assertEqual(self.run_hook(ev, d2, with_roles=False), "")

    def test_file_tools_through_entry_point(self):
        out_dir = outside_dir()
        self.addCleanup(shutil.rmtree, out_dir, True)
        existing = (Path(out_dir) / "f.txt").as_posix()
        Path(existing).write_text("x", encoding="utf-8")
        with tempfile.TemporaryDirectory() as d:
            for tool, key in (("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"),
                              ("NotebookEdit", "notebook_path")):
                ev = {"tool_name": tool, "tool_input": {key: existing}, "cwd": d}
                out = self.decision(self.run_hook(ev, d, role="engineer"))
                self.assertEqual(out["permissionDecision"], "deny", tool)
                self.assertIn("вне своей папки", out["permissionDecisionReason"])
                self.assertEqual(self.run_hook(ev, d), "", tool)              # сессия CEO/владельца — можно (аудит-3)
                inside = dict(ev, tool_input={key: d.replace("\\", "/") + "/src/x.rs"})
                self.assertEqual(self.run_hook(inside, d, role="engineer"), "", tool)
            new = {"tool_name": "Write", "tool_input": {"file_path": (Path(out_dir) / "new.txt").as_posix()}, "cwd": d}
            self.assertEqual(self.run_hook(new, d, role="engineer"), "")     # новый файл вне папки — создание
            nopath = {"tool_name": "Write", "tool_input": {"content": "x"}, "cwd": d}
            self.assertEqual(self.decision(self.run_hook(nopath, d))["permissionDecision"], "deny")
        with tempfile.TemporaryDirectory() as d2:                           # без команды ролей — молчит и для файлов
            ev = {"tool_name": "Write", "tool_input": {"file_path": existing}, "cwd": d2}
            self.assertEqual(self.run_hook(ev, d2, with_roles=False), "")

    def test_publication_consent_through_entry_point(self):
        with tempfile.TemporaryDirectory() as d:
            tickets = os.path.join(d, ".claude", "tickets")
            os.makedirs(tickets)
            os.makedirs(os.path.join(d, ".claude", "dispatcher"))                            # сюда страж пишет журнал
            ev = {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}, "cwd": d}
            self.assertEqual(self.run_hook(ev, d), "")                                       # сессия CEO — можно
            out = self.decision(self.run_hook(ev, d, role="engineer", ticket="TK-9"))        # тикета нет — отказ
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertIn("Публикация", out["permissionDecisionReason"])
            Path(tickets, "TK-9.md").write_text("---\nid: TK-9\nowner: engineer\n---\n", encoding="utf-8")
            self.assertEqual(self.decision(self.run_hook(ev, d, role="engineer", ticket="TK-9"))["permissionDecision"], "deny")
            Path(tickets, "TK-9.md").write_text("---\nid: TK-9\nowner: engineer\nallow: publish (В-3)\n---\n", encoding="utf-8")
            self.assertEqual(self.run_hook(ev, d, role="engineer", ticket="TK-9"), "")        # согласие в шапке — можно
            self.assertEqual(self.decision(self.run_hook(ev, d, role="engineer", ticket="TK-8"))["permissionDecision"], "deny")
            log = Path(d, ".claude", "dispatcher", "guard.log").read_text(encoding="utf-8")
            self.assertIn("\tpublish\tallow\t", log)
            self.assertIn("\tpublish\tdeny\t", log)

    def test_unreadable_event_is_refusal_in_a_roles_project(self):
        with tempfile.TemporaryDirectory() as d:
            for raw in ("не json", "", "[1, 2]"):
                out = self.decision(self.run_hook(None, d, raw=raw))
                self.assertEqual(out["permissionDecision"], "deny", raw)
                self.assertIn("Страж удаления упал", out["permissionDecisionReason"])
        with tempfile.TemporaryDirectory() as d2:
            self.assertEqual(self.run_hook(None, d2, with_roles=False, raw="не json"), "")

    def test_crash_inside_guard_is_refusal(self):
        from unittest import mock
        import io
        import json
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".claude", "roles"))
            ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": d})
            stdout = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(ev)), mock.patch.object(sys, "stdout", stdout), \
                    mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": d}), \
                    mock.patch.object(dg, "configure"), mock.patch.object(dg, "check_tool", side_effect=RuntimeError("boom")):
                self.assertEqual(dg.main(), 0)
            out = json.loads(stdout.getvalue())["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("RuntimeError", out["permissionDecisionReason"])

    # --- обёртка в hooks/hooks.json: отказ, когда интерпретатора нет или страж не запускается
    def guard_command(self):
        import json
        with open(os.path.join(self.ROOT, "hooks", "hooks.json"), encoding="utf-8") as fh:
            entries = json.load(fh)["hooks"]["PreToolUse"]
        guards = [e for e in entries if "delete_guard.py" in json.dumps(e)]
        self.assertEqual(len(guards), 1)
        for tool in ("Bash", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit"):
            self.assertIn(tool, guards[0]["matcher"].split("|"))
        return guards[0]["hooks"][0]["command"]

    def run_wrapper(self, plugin_root, path_dirs, event, project=None):
        """`project` — CLAUDE_PROJECT_DIR события; по умолчанию — свежий проект с `.claude/roles` (обёртка отказывает
        только в проекте команды ролей)."""
        import json
        import shutil as sh_
        import subprocess
        sh = sh_.which("sh")
        if not sh:
            self.skipTest("нет sh")
        if project is None:
            project = tempfile.mkdtemp(prefix="dg-proj-")
            self.addCleanup(shutil.rmtree, project, True)
            os.makedirs(os.path.join(project, ".claude", "roles"))
        cmd = self.guard_command().replace("${CLAUDE_PLUGIN_ROOT}", plugin_root.replace("\\", "/"))
        self.assertTrue(cmd.startswith("sh -c "))
        cmd = cmd.replace("sh -c ", '"' + sh.replace("\\", "/") + '" -c ', 1)         # PATH в тесте урезан: sh — по полному пути
        env = dict(os.environ, CLAUDE_PLUGIN_ROOT=plugin_root, PATH=os.pathsep.join(path_dirs), CLAUDE_PROJECT_DIR=project)
        return subprocess.run([sh, "-c", cmd], input=json.dumps(event).encode("utf-8"), capture_output=True, env=env)

    def test_wrapper_runs_guard(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".claude", "roles"))
            ev = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /etc/x"}, "cwd": d}
            r = self.run_wrapper(self.ROOT, os.environ["PATH"].split(os.pathsep), ev)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_wrapper_without_interpreter_is_refusal(self):
        with tempfile.TemporaryDirectory() as empty:
            r = self.run_wrapper(self.ROOT, [empty], {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": empty})
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("Страж удаления", r.stderr.decode("utf-8", "replace"))

    def test_wrapper_without_interpreter_outside_roles_project_is_silent(self):
        """Аудит-3 п.1: нет Python — отказ только в проекте команды ролей; в остальных проектах плагин не мешает."""
        import shutil as sh_
        tr = sh_.which("tr")
        with tempfile.TemporaryDirectory() as empty, tempfile.TemporaryDirectory() as plain:
            ev = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": plain}
            r = self.run_wrapper(self.ROOT, [empty], ev, project=plain)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), b"")
            if tr and os.name != "nt":                                      # путь Windows с обратными чертами → `/`
                os.symlink(tr, os.path.join(empty, "tr"))                   # в PATH только tr, Python по-прежнему нет
                with tempfile.TemporaryDirectory() as proj:
                    os.makedirs(os.path.join(proj, ".claude", "roles"))
                    r = self.run_wrapper(self.ROOT, [empty], ev, project=proj.replace("/", "\\"))
                    self.assertEqual(r.returncode, 2, r.stdout)

    def test_wrapper_with_broken_guard_is_refusal(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".claude", "hooks"))
            with open(os.path.join(root, ".claude", "hooks", "delete_guard.py"), "w", encoding="utf-8") as fh:
                fh.write("def main(:\n")                                    # синтаксическая ошибка
            r = self.run_wrapper(root, os.environ["PATH"].split(os.pathsep),
                                 {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": root})
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("Страж удаления", r.stderr.decode("utf-8", "replace"))


class Lexer(unittest.TestCase):
    def words(self, text):
        return [c.words for c in dg.tokenize(text)]

    def test_quotes_and_separators(self):
        self.assertEqual(self.words("echo 'a;b' \"c && d\"; ls"), [["echo", "a;b", "c && d"], ["ls"]])

    def test_redirects_dropped(self):
        self.assertEqual(self.words("rm x 2>/dev/null >out.txt"), [["rm", "x"]])

    def test_escaped_semicolon(self):
        self.assertEqual(self.words("find . -exec rm {} \\;"), [["find", ".", "-exec", "rm", "{}", ";"]])

    def test_heredoc_body_attached(self):
        cmds = dg.tokenize("cat <<EOF\nrm -rf /x\nEOF\nls")
        self.assertEqual([c.words for c in cmds], [["cat"], ["ls"]])
        self.assertEqual(cmds[0].heredocs, ["rm -rf /x"])

    def test_pipe_from(self):
        cmds = dg.tokenize("echo hi | python -")
        self.assertIs(cmds[1].pipe_from, cmds[0])

    def test_comment_skipped(self):
        self.assertEqual(self.words("ls # rm -rf /"), [["ls"]])

    def test_unterminated_raises(self):
        with self.assertRaises(dg.ParseError):
            dg.tokenize("echo 'abc")


class SettingsSource(unittest.TestCase):
    """CP8: удалённые машины — ключи `.claude/rpv.json` проекта; окружение (`RPV_GUARD_*`, прежние `ALPHA_*`) не читается."""

    HERE = os.path.dirname(os.path.abspath(__file__))

    def read(self, env_extra, rpv=None):
        import json
        import subprocess
        proj = tempfile.mkdtemp(prefix="dg-settings-")
        self.addCleanup(shutil.rmtree, proj, True)
        os.makedirs(os.path.join(proj, ".claude", "roles"))
        if rpv is not None:
            with open(os.path.join(proj, ".claude", "rpv.json"), "w", encoding="utf-8") as f:
                json.dump(rpv, f)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("RPV_", "ALPHA_"))}
        env.update(env_extra, CLAUDE_PROJECT_DIR=proj, PYTHONIOENCODING="utf-8")
        code = ("import delete_guard as d; print(d.STAGE, d.REMOTE_ROOTS, d.dispatcher_role(), "
                "d.closed_host('host-9.example'), d.PROTECTED_ERROR)")
        r = subprocess.run([sys.executable, "-c", code], cwd=self.HERE, env=env, capture_output=True, text=True,
                           encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_rpv_json_is_read_env_is_not(self):
        out = self.read({"ALPHA_GUARD_STAGE": "/dev/shm/old", "ALPHA_ROLE": "judge", "RPV_GUARD_STAGE": "/dev/shm/env",
                         "RPV_GUARD_REMOTE_ROOTS": "/srv/env/", "RPV_ROLE": "engineer"},
                        {"guard_stage": "/dev/shm/new", "guard_remote_roots": ["/srv/new/"],
                         "guard_forbidden_hosts": ["host-9.example"]})
        self.assertEqual(out, "/dev/shm/new ('/srv/new/',) engineer True None")
        self.assertEqual(self.read({"RPV_GUARD_STAGE": "/dev/shm/env"}), "() None False None")

    def test_bad_machine_keys_are_fail_closed(self):
        self.assertIn("guard_remote_roots — не список", self.read({}, {"guard_remote_roots": "/srv/new/"}))
        self.assertIn("guard_stage — не строка", self.read({}, {"guard_stage": ["/x"]}))

    def test_texts_name_settings_keys(self):
        for text in (dg.REASON, dg.REASON_OVERWRITE, dg.REASON_IRREVERSIBLE):
            self.assertNotIn("ALPHA_", text)
            self.assertNotIn("RPV_GUARD", text)
        self.assertIn("guard_remote_roots", dg.REASON)


class GuardGroups(unittest.TestCase):
    """Журнал стража (срез №5): решающая ветка проставляет метку группы; temp/legacy пишут и разрешения."""

    @classmethod
    def setUpClass(cls):
        cls.out = outside_dir()
        cls.f = os.path.join(cls.out, "a.txt")
        Path(cls.f).write_text("x")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def setUp(self):
        from unittest import mock
        p = mock.patch.dict(os.environ, {"RPV_ROLE": "engineer"})          # файловые правила — для запуска диспетчера
        p.start()
        self.addCleanup(p.stop)

    def trace(self, tool, **ti):
        tr = []
        reason = dg.check_tool({"tool_name": tool, "tool_input": ti, "cwd": CWD}, tr)
        return reason, tr

    def test_one_case_per_group(self):
        cases = {
            "rm": ("Bash", {"command": "rm -rf /c/Users/x"}),
            "py-ast": ("Bash", {"command": "python -c \"import shutil; shutil.rmtree('/c/Users/x')\""}),
            "ps": ("PowerShell", {"command": "del C:\\x"}),
            "ssh": ("Bash", {"command": "ssh srv@192.0.2.49 'rm -rf /home/other/x'"}),
            "tee-trunc-cp": ("Bash", {"command": f'echo a > "{self.f}"'}),
            "git": ("Bash", {"command": "git reset --hard"}),
            "publish": ("Bash", {"command": "git push origin main"}),
            "protected": ("Bash", {"command": "rm -rf data/raw/x"}),
            "file": ("Write", {"file_path": self.f}),
            "ceo-signal": ("Write", {"file_path": "C:/x/.claude/dispatcher/ceo-inbox.md"}),
            "legacy": ("Bash", {"command": "echo 'abc; rm -rf /c/Users/x"}),
        }
        with protect("data/raw"):
            for group, (tool, ti) in cases.items():
                reason, tr = self.trace(tool, **ti)
                self.assertIsNotNone(reason, group)
                self.assertEqual(tr[-1], (group, "deny"), (group, tr))

    def test_ps_inside_bash(self):
        reason, tr = self.trace("Bash", command='powershell -c "Remove-Item C:\\x"')
        self.assertIsNotNone(reason)
        self.assertEqual(tr[-1], ("ps", "deny"))

    def test_allows_are_journaled_for_temp_and_legacy_only(self):
        reason, tr = self.trace("Bash", command="rm -rf C:/Users/x/AppData/Local/Temp/abc/f")
        self.assertIsNone(reason)
        self.assertEqual(tr, [("temp", "allow")])
        reason, tr = self.trace("Edit", file_path="C:/Users/x/AppData/Local/Temp/anything/f.txt")
        self.assertIsNone(reason)
        self.assertEqual(tr, [("temp", "allow")])
        reason, tr = self.trace("Bash", command="echo 'abc")
        self.assertIsNone(reason)
        self.assertEqual(tr, [("legacy", "allow")])
        reason, tr = self.trace("Bash", command="rm -rf data/x")          # обычное разрешение — без записи
        self.assertIsNone(reason)
        self.assertEqual(tr, [])


if __name__ == "__main__":
    unittest.main()
