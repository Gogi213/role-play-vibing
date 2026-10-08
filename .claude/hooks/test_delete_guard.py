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

# проверки написаны на примере проекта alpha: корень и удалённые каталоги задаём окружением ДО импорта (умолчаний нет)
os.environ["CLAUDE_PROJECT_DIR"] = r"C:\visual projects\alpha"
os.environ["RPV_GUARD_REMOTE_ROOTS"] = "~/alpha/,$home/alpha/,${home}/alpha/,/home/deck/alpha/,/opt/alpha-compute/"
os.environ["RPV_GUARD_HOST_ROOTS"] = "203.0.113.3=/home/deck/alpha/,/root/tk0,/data/tk0,/tmp/"
os.environ["RPV_GUARD_STAGE"] = "/dev/shm/alpha-stage"
os.environ["RPV_GUARD_FORBIDDEN_HOSTS"] = "203.0.113.1"
os.environ["RPV_GUARD_HEAVY_HOST"] = "203.0.113.3"
os.environ["RPV_GUARD_HEAVY_HINT"] = "Обёртка замера: /data/benchrun.sh stand <команда> (волна — wave)."
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import delete_guard as dg  # noqa: E402

CWD = r"C:\visual projects\alpha"
SCRATCH = "C:/Users/3D6B~1/AppData/Local/Temp/claude/C--visual-projects-alpha/abc/scratchpad"


def denied(cmd, cwd=CWD):
    return dg.check(cmd, cwd) is not None


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
        self.ok('rm -rf "/c/visual projects/alpha/data/tmp"')

    def test_rm_in_project_windows_path(self):
        self.ok('rm -rf "C:\\visual projects\\alpha\\data\\tmp"')

    def test_rm_relative_in_project(self):
        self.ok("rm -f data/tmp/x.csv")

    def test_rm_in_scratchpad(self):
        self.ok(f'rm -rf "{SCRATCH}/sim"')

    def test_ssh_deck_alpha_subdir(self):
        self.ok("ssh deck@192.0.2.49 'rm -rf ~/alpha/tk026/stage'")

    def test_ssh_deck_ram_stage(self):
        self.ok("ssh deck@192.0.2.49 'rm -rf /dev/shm/alpha-stage'")

    def test_ssh_deck_ram_stage_inner(self):
        self.ok("ssh deck@192.0.2.49 'rm -rf /dev/shm/alpha-stage/2026-01-03/root'")

    def test_ssh_deck_ram_stage_glob(self):
        self.ok("ssh -i ~/.ssh/id_rsa -o BatchMode=yes deck@192.0.2.49 'rm -rf /dev/shm/alpha-stage/*'")

    def test_ssh_vps_compute_subdir(self):
        self.ok('ssh ubuntu@203.0.113.2 "rm -rf /opt/alpha-compute/tk025/tmp"')

    def test_ssh_cd_then_relative(self):
        self.ok("ssh deck@192.0.2.49 'cd ~/alpha/tk026 && rm -rf stage'")

    def test_systemd_run_wrapper(self):
        self.ok("ssh deck@192.0.2.49 \"systemd-run --user --unit=x bash -c 'rm -rf ~/alpha/tk026/stage'\"")

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
        self.ok("rsync -a /c/Windows/x deck@192.0.2.49:~/alpha/y/")

    def test_cd_then_rm_in_project_subdir(self):
        self.ok('cd "/c/visual projects/alpha/data" && rm -rf tmp')

    def test_find_delete_in_project(self):
        self.ok("find data/tmp -name '*.part' -delete")

    def test_find_exec_rm_in_project(self):
        self.ok("find data/tmp -name '*.part' -exec rm -f {} \\;")

    def test_python_rmtree_in_project(self):
        self.ok("python -c \"import shutil; shutil.rmtree('/c/visual projects/alpha/data/tmp')\"")

    def test_python_pathlib_in_project(self):
        self.ok("python - <<'EOF'\nfrom pathlib import Path\nPath('data/tmp/x.csv').unlink()\nEOF")

    def test_rm_variable_known_literal(self):
        self.ok('T="/c/visual projects/alpha/data/tmp"; rm -rf "$T"')

    def test_ssh_options_array(self):
        self.ok("K=(-i /c/Users/x/.ssh/id_rsa -o BatchMode=yes); H=deck@192.0.2.49\n"
                "ssh \"${K[@]}\" $H 'cd ~/alpha; rm -f tmp-p07/t9.finished; ls queue'")

    def test_ssh_command_variable_with_options(self):
        self.ok("S=\"ssh -o BatchMode=yes -i /c/Users/x/.ssh/id_rsa deck@192.0.2.49\"; $S 'rm -rf ~/alpha/tk026/stage'")

    def test_local_rm_with_ssh_elsewhere_in_command(self):
        self.ok('cd "/c/visual projects/alpha" && rm "tools/compute/old.sh" && S="ssh -i k deck@192.0.2.49"; $S ls')

    def test_git_rm_tracked_file(self):
        self.ok("git rm --cached tools/old.sh")

    def test_docker_rm_container(self):
        self.ok("docker rm -f alpha-test")

    def test_robocopy_copy_only(self):
        self.ok(r"robocopy data\a data\b /E")

    def test_perl_without_delete(self):
        self.ok("perl -e 'print 1'")

    def test_ssh_unknown_options_array_before_deck(self):
        self.ok("ssh \"${KEY[@]}\" deck@192.0.2.49 'rm -f ~/alpha/tmp-t38/bench14.DONE'")

    def test_rsync_remove_source_files_to_box_destination(self):
        self.ok("rsync -a --remove-source-files -e 'ssh -p 23 -i k' /opt/alpha-compute/x/ "
                "u1@u1.your-storagebox.de:alpha/x/")

    def test_powershell_assignment_of_here_string(self):
        self.ok("$script = @'\nrm -rf /tmp/x\n'@")

    def test_wrapper_script_with_host_arg_inside_policy(self):
        self.ok("/tmp/sshx deck@192.0.2.49 'cd ~/alpha && rm -f tmp-p07/read-a.log && ls'")

    def test_wrapper_script_collector_without_deletion(self):
        self.ok("/tmp/sshx ubuntu@203.0.113.1 'ls /opt/alpha/root | head'")

    def test_shell_function_wrapper_inside_policy(self):
        self.ok("rsh() { ssh -i k deck@192.0.2.49 \"$@\"; }\nrsh 'rm -rf ~/alpha/tk026/stage'")

    def test_echo_with_email_and_rm_text(self):
        self.ok("echo deck@192.0.2.49 'rm foo'")

    def test_script_creation_heredoc_over_ssh_is_data(self):
        self.ok("ssh deck@192.0.2.49 'cat > ~/alpha/x.sh <<\"EOF\"\nrm -rf /home/deck/other\nEOF\nchmod +x ~/alpha/x.sh'")

    def test_powershell_script_string_to_deck_inside_policy(self):
        self.ok("$script = @'\nrm -f ~/alpha/tmp-p07/x.log\n'@\nssh -i k deck@192.0.2.49 $script")

    def test_help_flag(self):
        self.ok("rm --help")

    def test_remove_item_whatif(self):
        self.ok("Remove-Item C:\\Windows\\x -Recurse -WhatIf")

    def test_remove_item_in_project(self):
        self.ok('Remove-Item -LiteralPath "C:\\visual projects\\alpha\\data\\tmp" -Recurse -Force')

    def test_two_commands_both_fine(self):
        self.ok("ls data && rm -f data/tmp/a.txt data/tmp/b.txt")


class Denied(unittest.TestCase):
    """Настоящее удаление вне своей папки или с непроверяемой целью — отказ."""

    def no(self, cmd, cwd=CWD):
        self.assertIsNotNone(dg.check(cmd, cwd), f"пропущено: {cmd!r}")

    # --- из задачи владельца
    def test_rm_users(self):
        self.no("rm -rf /c/Users/x")

    def test_ssh_collector(self):
        self.no("ssh ubuntu@203.0.113.1 'rm /opt/alpha/root/x.binlog'")

    def test_find_root_delete(self):
        self.no("find / -name x -delete")

    def test_python_rmtree_windows(self):
        self.no("python -c \"import shutil; shutil.rmtree('/c/Windows/x')\"")

    def test_remove_item_windows_single_backslash(self):
        self.no("Remove-Item C:\\Windows\\x -Recurse")

    def test_remove_item_windows_double_backslash(self):
        self.no("Remove-Item C:\\\\Windows\\\\x -Recurse")

    # --- Storage Box
    def test_storagebox_port23(self):
        self.no("ssh -p 23 u000000@u000000.your-storagebox.de 'rm -rf alpha/epochs/x'")

    def test_storagebox_port23_attached(self):
        self.no("ssh -p23 -i ~/.ssh/id_storagebox u000000@u000000.your-storagebox.de 'rm alpha/x'")

    def test_storagebox_option_port(self):
        self.no("ssh -o Port=23 -o BatchMode=yes u000000@u000000.your-storagebox.de 'rm -f alpha/x'")

    def test_storagebox_alpha_path_still_denied(self):
        self.no("ssh -p 23 u000000@u000000.your-storagebox.de 'rm -rf ~/alpha/x'")

    def test_storagebox_via_variable(self):
        self.no('SSHC="ssh -p 23 -i ~/.ssh/id_storagebox"; $SSHC u000000@u000000.your-storagebox.de "rm -f alpha/x"')

    def test_storagebox_heredoc_shell(self):
        self.no("ssh -p 23 u000000@u000000.your-storagebox.de <<'EOF'\nrm -rf alpha/epochs\nEOF")

    def test_rsync_delete_to_storagebox(self):
        self.no("rsync -a --delete -e 'ssh -p 23' data/x/ u000000@u000000.your-storagebox.de:alpha/x/")

    def test_collector_path_in_alpha_subdir(self):
        self.no("ssh ubuntu@203.0.113.1 'rm -rf /opt/alpha-compute/x'")

    # --- записи root/ deep/
    def test_root_segment(self):
        self.no('rm "/c/visual projects/alpha/data/root/x.binlog"')

    def test_deck_root_segment(self):
        self.no("ssh deck@192.0.2.49 'rm -rf ~/alpha/e-aug/root/2026-08-01'")

    def test_deep_segment(self):
        self.no("ssh deck@192.0.2.49 'rm -rf ~/alpha/deep/x'")

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
        self.no("ssh deck@192.0.2.49 \"bash -c 'rm -rf /home/deck/other'\"")

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
        self.no("ssh deck@192.0.2.49 python3 - <<'EOF'\nimport shutil\nshutil.rmtree('/home/deck/other')\nEOF")

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
        self.no('rm -rf "/c/visual projects/alpha/*"')

    def test_rm_project_root(self):
        self.no('rm -rf "/c/visual projects/alpha"')

    def test_rm_dot(self):
        self.no("rm -rf .")

    def test_rm_parent(self):
        self.no("rm -rf ../x")

    def test_rm_relative_after_cd_out(self):
        self.no("cd /c/Users/x && rm -rf data")

    def test_rm_relative_in_ssh(self):
        self.no("ssh deck@192.0.2.49 'rm -rf tk026/stage'")

    def test_rm_substitution_target(self):
        self.no("rm -rf $(cat list.txt)")

    def test_pipeline_remove_item(self):
        self.no("Get-ChildItem C:\\x | Remove-Item -Recurse")

    def test_remove_item_parenthesized(self):
        self.no("Remove-Item (Join-Path $env:TEMP 'x') -Recurse")

    def test_ssh_unknown_host_variable(self):
        self.no("ssh $BOX 'rm -f ~/alpha/x'")

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
        self.no("rsync -a --remove-source-files /c/Users/x/ deck@192.0.2.49:~/alpha/y/")

    def test_shred(self):
        self.no("shred -u /c/Users/x/secret.txt")

    def test_unterminated_quote_falls_back(self):
        self.no("rm -rf '/c/Users/x")

    def test_ps_here_string_python(self):
        self.no("@'\nimport shutil\nshutil.rmtree('/c/Users/x')\n'@ | python -")

    def test_ps_dotnet_delete(self):
        self.no("[System.IO.File]::Delete('C:\\Windows\\x')")

    def test_rclone_delete(self):
        self.no("rclone delete box:alpha/x")

    def test_wsl_rm(self):
        self.no("wsl rm -rf /mnt/c/Windows/x")

    def test_stage_lookalike(self):
        self.no("ssh deck@192.0.2.49 'rm -rf /dev/shm/alpha-stage-other'")

    def test_stage_dotdot(self):
        self.no("ssh deck@192.0.2.49 'rm -rf /dev/shm/alpha-stage/../x'")

    def test_ssh_options_array_collector(self):
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

    def test_ssh_unknown_options_array_before_collector(self):
        self.no("ssh \"${KEY[@]}\" ubuntu@203.0.113.1 'rm -f ~/alpha/x'")

    def test_ssh_host_is_unknown_variable(self):
        self.no("ssh $H 'rm -f ~/alpha/x'")

    def test_rsync_delete_to_remote_over_port_23(self):
        self.no("rsync -a --delete -e 'ssh -p 23' data/x/ somebox:alpha/x/")

    def test_wrapper_script_with_host_arg_outside_policy(self):
        self.no("/tmp/sshx deck@192.0.2.49 'rm -rf /home/deck/other'")

    def test_wrapper_script_collector_with_deletion(self):
        self.no("/tmp/sshx ubuntu@203.0.113.1 'rm -f /opt/alpha-compute/x'")

    def test_shell_function_wrapper_outside_policy(self):
        self.no("rsh() { ssh -i k deck@192.0.2.49 \"$@\"; }\nrsh 'rm -rf /home/deck/other'")

    def test_sftp_batch_on_storage_box(self):
        self.no("sftp -b - u1@u1.your-storagebox.de <<'EOF'\nrm alpha/x\nEOF")

    def test_powershell_script_string_to_collector(self):
        self.no("$script = @'\ncd /opt/alpha/src && rm -f tools/b5_grid.py\n'@\nssh -i k ubuntu@203.0.113.1 $script")

    def test_powershell_script_piped_to_remote_bash(self):
        self.no("$script = @'\nrm -rf /home/deck/other\n'@\n$script | ssh -i k deck@192.0.2.49 bash -s")

    def test_variable_command_word(self):
        self.no("$SSH deck@192.0.2.49 'rm -rf /home/deck/other'")


class NoNameExemption(unittest.TestCase):
    """A3(а), аудит 03.10: строка `tk020-dedupe-apply.sh` в команде отключала проверку целиком —
    `echo tk020-dedupe-apply.sh; rm -rf /` проходил. Исключения по имени скрипта больше нет."""

    def test_script_name_in_text_does_not_disable_check(self):
        for name in ("tk020-dedupe-apply.sh", "tk020-dedupe-apply.py"):
            self.assertIsNotNone(dg.check(f"echo {name}; rm -rf /c/Users/x", CWD), name)
            self.assertIsNotNone(dg.check(f"rm -rf /c/Users/x # {name}", CWD), name)
            self.assertIsNotNone(
                dg.check(f"ssh deck@192.0.2.49 'python3 {name} && rm -rf /home/deck/other'", CWD), name)

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
        self.ok('echo x > "/c/visual projects/alpha/data/out.txt"')
        self.ok(f'echo x > "{SCRATCH}/note.txt"')
        self.ok("ssh deck@192.0.2.49 'echo x > ~/alpha/tk026/out.log'")

    def test_redirect_remote_outside_roots(self):
        self.no("ssh deck@192.0.2.49 'echo x > /home/deck/other/f'")
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
        self.ok('OUT="/c/visual projects/alpha/data/o.txt"; echo x > "$OUT"')

    def test_redirect_to_root_or_deep_segment_remote(self):
        self.no("ssh deck@192.0.2.49 'echo x > ~/alpha/e-aug/root/x.binlog'")
        self.no("ssh deck@192.0.2.49 'echo x > ~/alpha/deep/x'")

    def test_redirect_to_storage_box_over_ssh(self):
        self.no("ssh -p 23 u000000@u000000.your-storagebox.de 'cat > alpha/x <<EOF\nx\nEOF'")

    # --- truncate
    def test_truncate(self):
        self.no(f'truncate -s 0 "{self.f}"')
        self.no(f'truncate --size=0 "{self.f}"')
        self.no("ssh deck@192.0.2.49 'truncate -s 0 /var/log/x.log'")

    def test_truncate_inside_roots(self):
        self.ok("truncate -s 0 data/x.log")
        self.ok("ssh deck@192.0.2.49 'truncate -s0 ~/alpha/x.log'")

    # --- dd
    def test_dd_of(self):
        self.no(f'dd if=/dev/zero of="{self.f}" bs=1 count=1')
        self.no("dd if=/dev/zero of=/dev/sda bs=1M")
        self.no("ssh deck@192.0.2.49 'dd if=a of=/home/deck/other/b'")

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
        self.ok("ssh deck@192.0.2.49 'cp a ~/alpha/b'")

    def test_cp_remote_outside_roots(self):
        self.no("ssh deck@192.0.2.49 'cp a /home/deck/other/b'")
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
        self.no("ssh deck@192.0.2.49 'cd ~/alpha && git clean -fd'")
        self.no("echo ok && git push --force")
        self.no('echo "$(git reset --hard)"')

    def test_text_mentions_are_not_commands(self):
        self.ok('git commit -m "не делать git reset --hard и git push --force"')
        self.ok("echo git clean -fd")
        self.ok("grep -n 'git push -f' README.md")


class CeoResources0310(unittest.TestCase):
    """Решение CEO 03.10 по ресурсам: (1) корни выделенного сервера, (2) git reset --hard / clean -f вне основного дерева,
    (3) автопамять проекта — запись/перезапись."""

    DED = "root@203.0.113.3"
    WT = SCRATCH + "/wt"
    MEM = "C--visual-projects-alpha/memory"

    def ok(self, cmd, cwd=CWD):
        reason = dg.check(cmd, cwd)
        self.assertIsNone(reason, f"ложный отказ: {cmd!r} → {reason}")

    def no(self, cmd, cwd=CWD):
        self.assertIsNotNone(dg.check(cmd, cwd), f"пропущено: {cmd!r}")

    # --- (1) выделенный сервер: /home/deck/alpha/, /root/tk0*, /data/tk0*, /tmp/
    def test_host_roots_delete(self):
        for path in ("/root/tk031/out", "/data/tk034/x", "/tmp/x", "/tmp/judge-g33/a.log", "/home/deck/alpha/tk031/y",
                     "/root/tk035raw10/plan10.txt"):
            self.ok(f"ssh -i ~/.ssh/id_rsa {self.DED} 'rm -rf {path}'")
        self.ok("ssh 203.0.113.3 'rm -f /tmp/x'")                     # без user@

    def test_host_roots_overwrite(self):
        self.ok(f"ssh {self.DED} 'echo x > /root/tk035new/imp.sh'")
        self.ok(f"ssh {self.DED} 'echo x > /tmp/dbg.out'")
        self.ok(f"ssh {self.DED} 'cp a.py /data/tk035/daily.py'")
        self.ok(f"ssh {self.DED} <<'EOF'\ncat > /root/tk037-probe.sh <<'X'\nid\nX\nEOF")
        self.ok(f"ssh {self.DED} 'truncate -s 0 /data/tk034/s.out'")

    def test_host_roots_with_cd_and_rsync(self):
        self.ok(f"ssh {self.DED} 'cd /data/tk034 && rm -rf out'")
        self.ok(f"ssh {self.DED} 'cd /tmp/work && rm -rf *.log'")
        self.ok(f"rsync -a --delete /x/ {self.DED}:/data/tk031/stage/")

    def test_host_data_copy_stays_closed(self):
        for path in ("/data/alpha/x", "/data/alpha/derived/deck-study", "/data/alpha/root/f"):
            self.no(f"ssh {self.DED} 'rm -rf {path}'")
        self.no(f"ssh {self.DED} 'echo x > /data/alpha/derived/y.csv'")
        self.no(f"ssh {self.DED} 'cd /data/alpha && rm -rf derived'")
        self.no(f"ssh {self.DED} 'mv new.csv /data/alpha/derived/y.csv'")
        self.no(f"rsync -a --delete /x/ {self.DED}:/data/alpha/derived/")

    def test_host_roots_are_not_the_roots_themselves_or_broader(self):
        for path in ("/root/.ssh", "/root/tk0", "/root/tk0/", "/data/tk0", "/tmp", "/tmp/", "/tmp/*", "/root",
                     "/data", "/etc/x", "/root/tk031/../.ssh", "/tmp/../data/alpha/x", "/root/tk031/root/x",
                     "/data/tk031/deep/x", "/home/deck/alpha"):
            self.no(f"ssh {self.DED} 'rm -rf {path}'")

    def test_host_roots_do_not_apply_to_other_hosts_or_local(self):
        for host in ("root@203.0.113.2", "deck@192.0.2.49", "root@203.0.113.1"):
            self.no(f"ssh {host} 'rm -rf /root/tk031/x'")
            self.no(f"ssh {host} 'rm -rf /data/tk031/x'")
        self.no("rm -rf /root/tk031/x")                                  # локально
        self.no("ssh root@203.0.113.1 'rm -rf /tmp/x'")                 # закрытый узел: и /tmp/ нельзя
        self.no("ssh $H 'rm -rf /tmp/x'")                                # хост не определён
        self.no(f"ssh {self.DED} 'ssh root@203.0.113.1 rm -rf /tmp/x'")  # вложенный ssh — уже другой хост

    # --- (2) git reset --hard / clean -f вне основного дерева
    def test_git_in_scratch_dir_by_cd_variable_and_git_c(self):
        self.ok(f'cd "{self.WT}" && git reset -q --hard abc123')
        self.ok(f'W="{self.WT}" && cd "$W" && git reset -q --hard abc123 && git apply t.patch')
        self.ok(f'cd "C:/visual projects/alpha" && W="{self.WT}" && git diff > "$W/t.patch" && cd "$W" && git reset --hard d1')
        self.ok(f'git -C "{self.WT}" reset --hard HEAD~1')
        self.ok(f'git -C "{self.WT}" clean -fdx')
        self.ok("git reset --hard", cwd=self.WT.replace("/", "\\"))      # рабочий каталог вызова — scratch
        self.ok("git clean -fd", cwd=self.WT)

    def test_git_in_remote_and_tmp_dirs(self):
        self.ok("ssh root@203.0.113.2 'cd /opt/alpha-compute/wt1 && git reset --hard X && git clean -fdx'")
        self.ok("ssh root@203.0.113.2 'git -C /opt/alpha-compute/wt1 reset --hard X'")
        self.ok("ssh root@203.0.113.2 'cd /tmp/wt && git clean -fd'")
        self.ok("cd /tmp/wt && git clean -fd")
        self.ok(f"ssh {self.DED} 'cd /root/tk035/wt && git reset --hard HEAD'")
        self.ok(f"ssh {self.DED} 'git -C /data/tk034/repo clean -fd'")

    def test_git_main_tree_still_refused(self):
        self.no('cd "C:/visual projects/alpha" && git reset --hard')
        self.no('cd "C:/visual projects/alpha/data" && git clean -fd')
        self.no('cd "C:/visual projects/alpha/.claude/wt" && git reset --hard')
        self.no('git -C "C:/visual projects/alpha" reset --hard', cwd=self.WT)
        self.no('git -C "/c/visual projects/alpha" clean -fd', cwd=self.WT)
        self.no("git reset --hard")                                       # рабочий каталог — основное дерево
        self.no(f'git -C "{self.WT}/../../../../../../../../visual projects/alpha" reset --hard')

    def test_git_unknown_or_indirect_dir_refused(self):
        self.no('cd $W && git reset --hard')                              # неизвестная переменная
        self.no('cd "$W" && git clean -fd')
        self.no('W=$(mktemp -d); cd "$W" && git reset --hard')
        self.no('git -C "$W" reset --hard', cwd=self.WT)
        self.no(f'GIT_DIR="C:/visual projects/alpha/.git" git -C "{self.WT}" reset --hard')
        self.no(f'export GIT_WORK_TREE="C:/visual projects/alpha"; cd "{self.WT}" && git reset --hard')
        self.no('git --git-dir="C:/visual projects/alpha/.git" reset --hard', cwd=self.WT)
        self.no('git --work-tree="C:/visual projects/alpha" reset --hard', cwd=self.WT)
        self.no('cd wt && git reset --hard', cwd=None)                    # относительный путь без известного cwd

    def test_git_remote_roots_themselves_and_other_places_refused(self):
        self.no("ssh deck@192.0.2.49 'cd ~/alpha && git clean -fd'")
        self.no("ssh root@203.0.113.2 'cd /opt/alpha-compute && git reset --hard'")
        self.no("ssh root@203.0.113.2 'cd /root/x && git reset --hard'")
        self.no("ssh root@203.0.113.2 'cd /tmp && git reset --hard'")
        self.no("cd /tmp && git reset --hard")
        self.no("ssh root@203.0.113.1 'cd /tmp/wt && git reset --hard'")      # коллектор — закрытый узел
        self.no("ssh root@203.0.113.2 'cd /tmp/wt/../../root/x && git clean -fd'")

    def test_git_push_force_always_refused(self):
        self.no(f'cd "{self.WT}" && git push --force')
        self.no("git push -f", cwd=self.WT)
        self.no("ssh root@203.0.113.2 'cd /opt/alpha-compute/wt1 && git push --force-with-lease'")

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
                     "C:\\Users\\user\\.claude\\projects\\C--visual-projects-alpha\\memory"):
            self.ok(f"echo x > {base}/MEMORY.md")
            self.ok(f"echo x >| {base}/alpha-state.md")
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
        self.no("ssh deck@192.0.2.49 'echo x > ~/.claude/projects/" + self.MEM + "/x.md'")   # удалённый ~ — другой
        with tempfile.TemporaryDirectory(prefix="dg-mem-", dir=str(Path.home())) as d:
            base = Path(d) / ".claude" / "projects"
            for proj in ("c--other-project", "c--visual-projects-alpha"):
                (base / proj / "memory").mkdir(parents=True)
                (base / proj / "memory" / "x.md").write_text("x", encoding="utf-8")
            # существующие файлы: чужая автопамять и та же, но не под домашним каталогом — отказ
            self.no(f"echo x > {(base / 'c--other-project' / 'memory' / 'x.md').as_posix()}")
            self.no(f"echo x > {(base / 'c--visual-projects-alpha' / 'memory' / 'x.md').as_posix()}")
        self.assertFalse(dg.write_only_ok("~/.claude/projects/C--visual-projects-alpha/memory/../../../settings.json"))
        self.assertFalse(dg.write_only_ok("~/.claude/projects/C--visual-projects-alpha/memory"))   # сам каталог — нет
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
    def test_mv_deep_then_rm_is_refused_at_the_move(self):
        self.no('mv "/c/visual projects/alpha/data/deep/x.bin" ./trash && rm -rf ./trash')
        self.no("mv data/deep/x.bin ./trash && rm -rf ./trash")
        self.no("mv data/root/2026-08-01 data/old && ls")
        self.no("ssh deck@192.0.2.49 'mv ~/alpha/e-aug/root/x ~/alpha/trash'")

    def test_mv_source_outside_roots(self):
        self.no("mv /c/Users/x/a.bin data/")
        self.no("mv -n /c/Users/x/a.bin data/new.bin")                # -n охраняет приёмник, источник всё равно исчезает
        self.no("mv -t data/old /c/Users/x/a data/b")
        self.no("mv data/a /c/Users/x/b data/old/")                   # хотя бы один источник вне папки
        self.no("sudo mv /c/Users/x/a data/")
        self.no("bash -c 'mv /c/Users/x/a data/'")
        self.no("ssh deck@192.0.2.49 'mv /home/deck/other/a ~/alpha/x'")

    def test_mv_unknown_or_stdin_sources(self):
        self.no('mv "$f" data/')
        self.no("ls | xargs mv -t data/old")
        self.no("ls data/tmp | xargs -I{} mv {} data/old/")
        self.no("mv $(cat list.txt) data/old/")

    def test_mv_inside_roots_is_fine(self):
        self.ok("mv data/a.bin data/b.bin")
        self.ok("mv -t data/old data/a data/b")
        self.ok("mv data/tmp/*.csv data/old/")
        self.ok('mv "/c/visual projects/alpha/data/a" "/c/visual projects/alpha/data/b"')
        self.ok(f'mv data/a "{SCRATCH}/b"')
        self.ok("ssh deck@192.0.2.49 'mv ~/alpha/tk026/a ~/alpha/tk026/b'")
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
        self.no("Move-Item data\\deep\\a.txt data\\b.txt")
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
        for path in ("C:\\visual projects\\alpha\\src\\lob\\x.rs", "C:/visual projects/alpha/docs/a.md",
                     "/c/visual projects/alpha/.claude/roles/notes/ceo.md", f"{SCRATCH}/p.py", "data/out.txt"):
            self.assertIsNone(self.tool("Write", path), path)
            self.assertIsNone(self.tool("Edit", path), path)
        self.assertIsNone(self.tool("MultiEdit", "src/lob/x.rs", cwd="C:/visual projects/alpha"))

    def test_file_tools_memory_and_temp(self):
        mem = "C:/Users/x/.claude/projects/C--visual-projects-alpha/memory/MEMORY.md"
        self.assertIsNone(self.tool("Write", mem))
        self.assertIsNone(self.tool("Edit", "C:/Users/x/AppData/Local/Temp/anything/f.txt"))
        self.assertIsNone(self.tool("Write", "/tmp/note.txt"))

    def test_file_tools_forbidden_segments_in_project(self):
        from unittest import mock
        with tempfile.TemporaryDirectory(prefix="dg-proj-") as d:
            base = dg.norm(Path(d).as_posix())
            (Path(d) / "data" / "root").mkdir(parents=True)
            (Path(d) / "data" / "root" / "x.bin").write_text("x", encoding="utf-8")
            with mock.patch.object(dg, "LOCAL_ROOTS", (base + "/",)):
                self.assertIsNotNone(self.tool("Edit", f"{base}/data/root/x.bin", cwd=base))      # единственная копия
                self.assertIsNotNone(self.tool("Write", f"{base}/data/root/x.bin", cwd=base))
                self.assertIsNone(self.tool("Write", f"{base}/data/ok.txt", cwd=base))

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
        self.no("ssh deck@192.0.2.49 'echo x | tee /home/deck/other/f'")
        self.no(f"bash -c 'echo x | tee \"{self.f}\"'")

    def test_tee_append_new_inside_harmless_are_fine(self):
        self.ok(f'echo x | tee -a "{self.f}"')
        self.ok(f'echo x | tee --append "{self.f}"')
        self.ok(f'echo x | tee -ia "{self.f}"')
        self.ok(f'echo x | tee "{self.new}"')
        self.ok("echo x | tee data/out.txt")
        self.ok("echo x | tee /dev/null")
        self.ok("echo x | tee")
        self.ok("ssh deck@192.0.2.49 'echo x | tee ~/alpha/tk026/o.log'")

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
                    "ssh deck@192.0.2.49 'cd ~/alpha && git checkout -- x'", "echo ok && git branch -D x"):
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
                    "git push origin main", "git push origin HEAD:refs/heads/x", "git push -u origin feature",
                    "git push --tags", "git checkout --help", "git restore --help"):
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
            self.no(f'cd "C:/visual projects/alpha" && {cmd}')
        self.ok("ssh root@203.0.113.2 'cd /opt/alpha-compute/wt1 && git checkout -- . && git stash drop'")
        self.no("ssh root@203.0.113.2 'cd /opt/alpha-compute && git checkout -- .'")
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
        for host in ("root@203.0.113.2", "deck@192.0.2.49", "root@203.0.113.3"):
            self.ok(f"ssh {host} 'rm -rf /tmp/x'")
            self.ok(f"ssh {host} 'echo x > /tmp/dbg.out'")
            self.ok(f"ssh {host} 'cd /tmp/work && rm -rf out'")
            self.ok(f"ssh {host} 'echo x > $TMP/y; rm -f $TMPDIR/y'")
            self.no(f"ssh {host} 'rm -rf /tmp'")
            self.no(f"ssh {host} 'rm -rf /tmp/*'")
            self.no(f"ssh {host} 'rm -rf /tmp/../etc/x'")

    def test_temp_not_in_closed_nodes(self):
        self.no("ssh root@203.0.113.1 'rm -rf /tmp/x'")                      # коллектор
        self.no("ssh root@203.0.113.1 'echo x > /tmp/x'")
        self.no("ssh -p 23 u1@u1.your-storagebox.de 'rm -rf /tmp/x'")
        self.no("ssh $H 'rm -rf /tmp/x'")                                     # хост не определён
        self.no("ssh $H 'echo x > $TMP/x'")

    def test_temp_roots_themselves_dotdot_glob_and_segments_stay_closed(self):
        for target in ("$TMP", "$TEMP/", "/tmp", "/tmp/", "/tmp/*", "$TMP/*", "$TMP/../x", "/tmp/../etc/x",
                       "/tmp/x/../../etc", "$TEMP/..", "C:/Users/x/AppData/Local/Temp/../Documents/f",
                       "/tmp/data/root/x", "$TMP/deep/x", "/tmp/root", "${TMP}/../x", "$env:TEMP\\..\\x"):
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
        self.ok("ssh deck@192.0.2.49 'W=$(mktemp -d); rm -rf $W/x'")
        self.ok("mktemp -d")

    def test_mktemp_with_explicit_directory_is_not_temp(self):
        self.no("rm -rf $(mktemp -d -p /c/Users/x)/a")
        self.no("rm -rf $(mktemp -d --tmpdir=/c/Users/x)/a")
        self.no("rm -rf $(mktemp -d /c/Users/x/w.XXXX)/a")
        self.no('W=$(mktemp -d -p /c/Users/x); rm -rf "$W/a"')
        self.no("rm -rf $(mktemp -d)/../a")
        self.no("rm -rf $(mktemp -d)/root/a")
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
        (base / "deep").mkdir()
        (base / "deep" / "x.bin").write_text("x", encoding="utf-8")
        cls.settings = (base / "settings.json").as_posix()
        cls.repo_file = (base / "repo" / "main.py").as_posix()
        cls.deep_file = (base / "deep" / "x.bin").as_posix()

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

    def test_ceo_session_still_never_root_deep(self):
        self.as_role(None)
        reason = self.tool("Edit", self.deep_file)
        self.assertIsNotNone(reason)
        self.assertIn("никогда", reason)
        self.assertIsNone(self.tool("Write", self.out + "/deep/new.bin"))      # нового файла ещё нет — создание

    def test_dispatcher_role_keeps_full_rule(self):
        for role in ("engineer", "researcher", "judge"):
            self.as_role(role)
            self.assertIsNotNone(self.tool("Edit", self.repo_file), role)
            self.assertIn("вне своей папки", self.tool("Write", self.settings), role)

    def test_unknown_role_value_is_ceo_session(self):
        self.as_role("ceo")                                                # не роль диспетчера — правила сессии CEO
        self.assertIsNone(self.tool("Edit", self.repo_file))

    def test_home_root_user_is_not_root_segment(self):
        from unittest import mock
        self.as_role(None)
        with mock.patch.object(dg.os.path, "expanduser", return_value="/root"):
            self.assertFalse(dg.hard_forbidden_file("/root/.claude/settings.json", dg.Ctx(None)))
            self.assertTrue(dg.hard_forbidden_file("/root/data/root/x.bin", dg.Ctx(None)))

    # ------------------------------------------------------------------------------------------ (ж) .git
    def test_git_dir_deletion_refused_for_everyone(self):
        for role in (None, "engineer"):
            self.as_role(role)
            for cmd in ("rm -rf .git", "rm -rf ./.git/", 'rm -rf "C:/visual projects/alpha/.git"', "rm -rf .*",
                        "rm -rf .[!.]*", "rm -rf .g*", "rm -f .git/config", "rm -rf .git/objects",
                        "mv .git /c/Users/x/old-git", "cd .git && rm -rf objects", "find .git -delete",
                        "ssh deck@192.0.2.49 'rm -rf ~/alpha/repo/.git'", "Remove-Item -Recurse .git"):
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
        self.assertIsNotNone(self.tool("Edit", "C:/visual projects/alpha/.git/config"))
        self.assertIsNotNone(dg.check("echo ref > .git/HEAD", CWD))
        self.as_role(None)                                                 # CEO: правка .git/hooks и т. п. — можно
        self.assertIsNone(self.tool("Edit", "C:/visual projects/alpha/.git/hooks/pre-commit"))
        self.assertIsNone(dg.check("echo ref > .git/info/exclude", CWD))

    # ------------------------------------------------------------------------------------------ (з) настройки
    def test_settings_closed_to_dispatcher_roles(self):
        self.as_role("engineer")
        for name in ("Write", "Edit", "MultiEdit"):
            for path in ("C:/visual projects/alpha/.claude/settings.json", ".claude/settings.local.json",
                         "C:\\visual projects\\alpha\\.claude\\settings.local.json"):
                reason = self.tool(name, path)
                self.assertIsNotNone(reason, f"{name} {path}")
                self.assertIn("Настройки Claude Code", reason)
        for cmd in ("echo '{}' > .claude/settings.json", "cp /c/x/s.json .claude/settings.local.json",
                    "rm .claude/settings.json", "mv .claude/settings.json data/old.json",
                    "cat x | tee .claude/settings.local.json"):
            self.assertIn("Настройки Claude Code", dg.check(cmd, CWD) or "", cmd)
        self.assertIsNone(self.tool("Write", "C:/visual projects/alpha/.claude/roles/notes/engineer.md"))
        self.assertIsNone(dg.check("cat .claude/settings.json", CWD))

    def test_settings_open_to_ceo(self):
        self.as_role(None)
        self.assertIsNone(self.tool("Edit", "C:/visual projects/alpha/.claude/settings.json"))
        self.assertIsNone(dg.check("echo '{}' > .claude/settings.local.json", CWD))


class BenchLock(unittest.TestCase):
    """Замок замеров (CEO 06.10): на машине замеров (RPV_GUARD_HEAVY_HOST) тяжёлые команды из ssh-сессии (du, find, python-скрипты,
    md5sum/cmp по каталогам, tar, rsync, cp -r, vmtouch, cat бинлогов) — отказ, если не обёрнуты в systemd-run и не идут через
    /data/benchrun.sh | /data/tk052/benchrun2.sh. Короткие чтения, systemctl, выкладка `cat > файл && mv` — свободно."""

    DED = "root@203.0.113.3"
    SSH = "ssh -i ~/.ssh/id_rsa -o UserKnownHostsFile=~/.ssh/known_hosts " + DED
    MARK = "Тяжёлая команда на машине замеров"

    def remote(self, text, prefix=None):
        import shlex
        return f"{prefix or self.SSH} {shlex.quote(text)}"

    def no(self, text, **kw):
        reason = dg.check(self.remote(text, **kw), CWD)
        self.assertIsNotNone(reason, f"пропущено: {text!r}")
        self.assertIn(self.MARK, reason, f"отказ по другой причине: {text!r} → {reason}")

    def ok(self, text, **kw):
        reason = dg.check(self.remote(text, **kw), CWD)
        self.assertIsNone(reason, f"ложный отказ: {text!r} → {reason}")

    # --- запрещено (инциденты 06.10: find, du -sh /data/tk046, python3 tk040-complete.py)
    def test_incident_commands(self):
        self.no("du -sh /data/tk046")
        self.no("find /data/tk046 -name '*.out' -newer /data/x")
        self.no("cd /data/tk040 && python3 tk040-complete.py")
        self.no("nohup python3 -u /data/tk040/tk040-complete.py > /data/tk040/c.log 2>&1 &")

    def test_other_heavy_tools(self):
        for cmd in ("rsync -a /data/tk046/ /data/tk047/", "cp -r /data/tk046/two /data/tk047/", "cp -a /data/tk046 /data/x",
                    "cp -Rp /data/tk046 /data/x", "cp --recursive /data/tk046 /data/x", "vmtouch -t /data/alpha/epochs",
                    "tar czf /data/x.tgz /data/tk046", "tar -tf /data/x.tar", "tar xf /data/tk046/x.tar",
                    "ls -lR /data/tk046", "grep -r foo /data/tk046", "ncdu /data", "tree /data",
                    "diff -r /data/tk046/a /data/tk046/b", "diff -rq /data/a /data/b"):
            with self.subTest(cmd=cmd):
                self.no(cmd)

    def test_hash_and_compare_over_directories_masks_lists(self):
        for cmd in ("md5sum /data/tk046/two/*", "sha256sum /data/tk046/two/", "sha256sum /data/tk046/*/*.out",
                    "b3sum -c /data/tk046/sums.b3", "sha256sum --check /data/x.sum",
                    "ls /data/tk046 | xargs sha256sum", "find /data/a -type f | xargs -P8 md5sum",
                    "for f in /data/a/*.out; do cmp $f /data/b/${f##*/}; done", "md5sum $F",
                    "sha256sum /data/alpha/epochs/e-aug/root/HYPEUSDT-2026-08-20.binlog",
                    "cmp /data/alpha/a.binlog /data/alpha/b.binlog", "md5sum /data/tk046/x.abin"):
            with self.subTest(cmd=cmd):
                self.no(cmd)

    def test_cat_of_big_binlogs(self):
        self.no("cat /data/alpha/epochs/e-aug/root/HYPEUSDT-2026-08-20.binlog | md5sum")
        self.no("cat /data/alpha/epochs/e-aug/root/*.binlog | wc -c")
        self.no("cat /data/alpha/epochs/e-aug/root/*")
        self.no("cp /data/alpha/epochs/e-aug/root/X.binlog /data/tk040/")
        self.ok("cat /data/alpha/root/instruments.csv")
        self.ok("cat /data/tk040/progress.txt /data/tk0*/x.csv")

    def test_python_scripts(self):
        for cmd in ("python3 /data/tk040/x.py", "python3 -u /data/tk040/x.py", "python x.py", "python3.11 -B x.py",
                    "/usr/bin/python3 /data/x.py arg", "python3 -W ignore /data/x.py", "env A=1 python3 x.py",
                    "timeout 600 python3 /data/x.py", "sudo -u nobody nice python3 /data/x.py", "python3 -m cProfile x.py",
                    "python3 -m http.server", "python3 -c '" + "x=1;" * 60 + "'", "python3", "python3 -",
                    "python3 - <<'EOF'\n" + "y = 2\n" * 60 + "EOF"):
            with self.subTest(cmd=cmd):
                self.no(cmd)

    def test_python_short_forms_allowed(self):
        for cmd in ("python3 -m py_compile /data/tk048/registry/snap.py.new", "python3 -c 'print(1)'", "python3 --version",
                    "python3 -c 'import json; print(json.load(open(\"/data/tk040/p.json\"))[\"n\"])'",
                    "python3 - <<'EOF'\nprint(1)\nEOF", "python3 -c \"" + "x" * 190 + "\""):
            with self.subTest(cmd=cmd):
                self.ok(cmd)

    def test_tar_extract_from_stdin_is_deploy(self):
        self.ok("tar xzf - -C /data/tk040")
        self.ok("tar -xf - -C /data/tk040")
        self.ok("tar x -C /data/tk040")
        self.ok("tar -xzvf - -C/data/tk040 && ls -l /data/tk040")
        self.ok("tar --extract --file=- -C /data/tk040")
        self.no("tar xzf /data/tk046/x.tgz -C /data/tk040")
        self.no("tar czf - /data/tk046")
        self.no("tar --create --file - /data/tk046")
        self.no("tar xzf - -C /data/tk040 && tar czf /data/y.tgz /data/tk040")

    def test_short_reads_and_service_commands_allowed(self):
        for cmd in ("tail -n 50 /data/tk040/run.log", "tail -f /data/tk040/run.log", "head -3 /data/tk0*/x.csv",
                    "ls -l /data/tk040 /data/alpha/epochs/e-aug/root | head", "grep -c ^ETHUSDT /data/alpha/root/instruments.csv",
                    "grep -n err /data/tk0*/*.log", "cat /data/tk040/progress.txt", "systemctl status tk040-run --no-pager",
                    "systemctl list-units 'tk0*' --state=active", "systemctl freeze tk040-run", "uptime", "free -g; df -h /data",
                    "journalctl -u tk040-run -n 50 --no-pager", "pgrep -af python3", "ps aux | grep python3",
                    "sha256sum /data/tk048/benchrun.sh /opt/alpha-compute/bin/alpha-b14", "md5sum /data/tk048/x.bin",
                    "bash -n /data/tk048/benchrun.sh.new", "diff /data/tk040/a.txt /data/tk040/b.txt",
                    "cmp /data/tk040/a.out /data/tk040/b.out", "echo $(date) | md5sum", "echo 'du -sh /data' > /data/tk040/n.txt",
                    "cp /data/tk035raw/plan10.txt /root/tk035raw10/plan10.txt", "mkdir -p /data/tk040 && ls /data/tk040"):
            with self.subTest(cmd=cmd):
                self.ok(cmd)

    def test_deploy_cat_and_mv_still_works(self):
        self.ok("cat > /data/tk040/x.py.new && python3 -m py_compile /data/tk040/x.py.new && mv /data/tk040/x.py.new /data/tk040/x.py")
        self.ok("mkdir -p /data/tk048/registry && cat > /data/tk048/registry/snap.py.new && chmod +x /data/tk048/registry/snap.py.new")
        self.ok("cat > /data/tk048/benchrun.sh.new && bash -n /data/tk048/benchrun.sh.new && mv /data/tk048/benchrun.sh.new /data/tk048/benchrun.sh")
        self.ok("cat > /data/tk040/g.py <<'EOF'\nimport os\nos.walk('/data')\nEOF\nchmod +x /data/tk040/g.py")
        self.assertIsNone(dg.check(f"cat tools/registry/snap.py | {self.SSH} 'cat > /data/tk048/registry/snap.py.new && "
                                   "python3 -m py_compile /data/tk048/registry/snap.py.new'", CWD))
        self.assertIsNone(dg.check(f"tar -cf - -C tools/compute p08-jul.py | {self.SSH} 'tar -xf - -C /data/tk040'", CWD))

    # --- разрешено под замком: systemd-run и benchrun
    def test_systemd_run_exempts_the_wrapped_command(self):
        for cmd in ("systemd-run --unit tk046-du -p CPUQuota=100% --collect du -sh /data/tk046",
                    "systemd-run --unit tk040-c --collect -p CPUQuota=200% python3 /data/tk040/tk040-complete.py",
                    "systemd-run --unit=tk040-c --collect --property=CPUQuota=200% find /data/tk046 -name '*.out'",
                    "nohup systemd-run --unit tk040-y python3 /data/tk040/y.py &",
                    "systemd-run --unit a --collect bash -c 'python3 /data/x.py; find /data -name x; du -sh /data'",
                    "sudo systemd-run --unit t -p CPUQuota=400% --wait --pipe md5sum /data/tk046/two/*"):
            with self.subTest(cmd=cmd):
                self.ok(cmd)

    def test_benchrun_exempts(self):
        for cmd in ("/data/benchrun.sh stand python3 /data/tk040/x.py", "/data/tk052/benchrun2.sh wave du -sh /data/tk046",
                    "systemd-run --unit tk040-x --collect -p CPUQuota=800% /data/benchrun.sh stand bash -c "
                    "'python3 /data/tk040/x.py > /data/tk040/x.out'",
                    "systemd-run --unit tk052-x /data/tk052/benchrun2.sh wave /opt/alpha-compute/bin/alpha-b14 run",
                    "bash /data/benchrun.sh stand find /data/tk046 -name '*.out'"):
            with self.subTest(cmd=cmd):
                self.ok(cmd)

    def test_exemption_is_per_command(self):
        self.no("systemd-run --unit tk046-a du -sh /data/tk046; du -sh /data/tk046")
        self.no("systemd-run --unit a --collect python3 /data/x.py && python3 /data/y.py")
        self.no("/data/benchrun.sh stand true; python3 /data/y.py")
        self.no("systemd-run --unit a true | xargs du -sh")

    # --- обёртки и формы вызова ssh
    def test_wrappers_and_nesting(self):
        for cmd in ("bash -c 'du -sh /data'", "sh -c \"python3 /data/x.py\"", "ionice -c3 nice du -sh /data/tk046",
                    "sudo -u nobody du -sh /data", "echo $(du -sh /data)", "du -sh /data/tk046 | sort -h | tail",
                    "cd /data/tk040; ls; python3 run.py", "env A=1 xargs -n1 md5sum < list.txt", "time find /data -type f",
                    "busybox du -sh /data", "command du -sh /data", "exec python3 /data/x.py"):
            with self.subTest(cmd=cmd):
                self.no(cmd)

    def test_ssh_forms(self):
        d = self.DED
        for cmd in (f"ssh {d} du -sh /data/tk046", f"ssh -o BatchMode=yes -p 22 {d} 'echo hi; du -sh /data'",
                    "ssh 203.0.113.3 'du -sh /data'", f"ssh $K {d} 'find /data -name x'",
                    f"S='ssh -i k {d}'; $S 'du -sh /data/tk046'", f"K='-i k'; ssh $K {d} python3 /data/x.py",
                    f"/tmp/sshx {d} 'du -sh /data'", f"ssh {d} <<'EOF'\ncd /data/tk040\npython3 x.py\nEOF",
                    f"ssh {d} python3 - <<'EOF'\n" + "z = 3\n" * 80 + "EOF",
                    f"ssh {d} 'du -sh /data/tk046' | tee /tmp/du.txt",
                    f"powershell -Command \"ssh {d} 'du -sh /data'\"", f"ssh {d} 'du -sh /data/tk046"):  # незакрытая кавычка
            with self.subTest(cmd=cmd):
                reason = dg.check(cmd, CWD)
                self.assertIsNotNone(reason, cmd)
                self.assertIn(self.MARK, reason, cmd)

    def test_other_hosts_and_local_untouched(self):
        for host in ("root@203.0.113.9", "deck@192.168.1.49"):
            self.assertIsNone(dg.check(f"ssh {host} 'du -sh /opt; find /opt -name x; python3 x.py; md5sum /opt/a/*'", CWD), host)
        self.assertIsNone(dg.check("du -sh data; find . -name '*.rs'; python x.py; md5sum data/*; tar czf /tmp/x.tgz data", CWD))
        self.assertIsNone(dg.check(f"rsync -a data/ {self.DED}:/data/tk040/x/", CWD))   # локальная сторона: не удалённая команда
        self.assertIsNone(dg.check(f"echo 'ssh {self.DED} du -sh /data' > /tmp/note.txt", CWD))   # текст, не вызов

    def test_reason_explains_how_to(self):
        reason = dg.check(self.remote("du -sh /data/tk046"), CWD)
        for word in ("du", "замок", "не видит ssh-сессии", "systemd-run --unit", "CPUQuota", "py_compile", "200 символов",
                     "/data/benchrun.sh stand"):      # последнее — из RPV_GUARD_HEAVY_HINT
            self.assertIn(word, reason)

    def test_deletion_rules_still_first_class(self):
        reason = dg.check(self.remote("rm -rf /data/alpha/x"), CWD)
        self.assertIsNotNone(reason)
        self.assertNotIn(self.MARK, reason)
        self.assertIsNone(dg.check(self.remote("rm -rf /data/tk046/old"), CWD))

    def test_systemd_run_short_collect_flag_has_no_value(self):
        """`-G` у systemd-run — `--collect` без значения: раньше он «съедал» команду и `systemd-run -G rm -rf …` проходил."""
        for cmd in ("systemd-run -G rm -rf /c/Users/x/data", "systemd-run -G --unit a rm -rf /c/Users/x/data",
                    "sudo systemd-run -G -p CPUQuota=100% rm -rf /etc/x", "systemd-run --collect rm -rf /c/Users/x/data"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(dg.check(cmd, CWD), cmd)
        self.assertIsNotNone(dg.check(self.remote("systemd-run -G rm -rf /data/alpha/x"), CWD))
        self.assertIsNone(dg.check(self.remote("systemd-run -G --unit tk046-x rm -rf /data/tk046/old"), CWD))

    def test_heavy_allow(self):
        import re
        import heavy_guard as bg
        cmd = "python3 /data/sched/alsched.py submit --cls prod --name n -- /x/y.py"
        self.assertIsNotNone(self.deny_heavy(cmd))
        old = bg.HEAVY_ALLOW
        try:
            bg.HEAVY_ALLOW = bg._compile_allow(r"^python3 /data/sched/alsched\.py (submit|status)( |$)")
            self.assertIsNone(self.deny_heavy(cmd))
            self.assertIsNone(bg.heavy_label("python3", ["/data/sched/alsched.py", "status"]))
            self.assertIsNotNone(bg.heavy_label("python3", ["/data/sched/alsched.py", "evil"]))
            self.assertIsNotNone(bg.heavy_label("python3", ["/tmp/x.py", "/data/sched/alsched.py", "submit"]))
            self.assertIsNotNone(self.deny_heavy("python3 /data/sched/alsched.py submit x; python3 /tmp/y.py"))
        finally:
            bg.HEAVY_ALLOW = old
        self.assertIsNone(bg._compile_allow(""))
        self.assertIsNone(bg._compile_allow("(["))

    def deny_heavy(self, text):
        return dg.check(self.remote(text), CWD)

    def test_heavy_label_unit(self):
        import heavy_guard as bg
        self.assertEqual(bg.heavy_label("du", ["-sh", "/x"]), "du")
        self.assertIsNone(bg.heavy_label("ls", ["-l", "/data/tk046"]))
        self.assertEqual(bg.heavy_label("python3", ["/x.py"]), "python3 /x.py")
        self.assertIsNone(bg.heavy_label("python3", ["-c", "print(1)"]))
        self.assertIsNone(bg.heavy_label("python3", ["-"], ["print(1)"]))
        self.assertIsNotNone(bg.heavy_label("python3", ["-"], ["x=1\n" * 100]))
        self.assertIsNotNone(bg.heavy_label("python3", ["-"], []))
        self.assertIsNone(bg.heavy_label("sha256sum", ["/a/b.sh", "-"]))
        self.assertIsNotNone(bg.heavy_label("sha256sum", [], xargs=True))
        self.assertIsNone(bg.heavy_label("tar", ["xzf", "-", "-C", "/d"]))
        self.assertIsNotNone(bg.heavy_label("tar", ["czf", "-", "/d"]))
        self.assertIsNone(bg.heavy_label("tail", ["-n", "5", "/data/tk040/x.log"]))
        self.assertIsNone(bg.heavy_label("systemctl", ["status", "x"]))
        self.assertIsNotNone(bg.legacy_heavy("ssh root@203.0.113.3 'du -sh /x"))
        self.assertIsNone(bg.legacy_heavy("ssh root@203.0.113.3 'systemd-run du -sh /x"))
        self.assertIsNone(bg.legacy_heavy("ssh root@203.0.113.9 'du -sh /x"))



class HookEntryPoint(unittest.TestCase):
    """Точка входа PreToolUse: JSON со stdin → deny с причиной; не наш инструмент / нет `.claude/roles` в проекте — тишина;
    битое событие и сбой стража — отказ (fail-closed); обёртка в hooks.json — отказ, когда нет Python или страж сломан."""

    HERE = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.dirname(os.path.dirname(HERE))

    def run_hook(self, event, project, with_roles=True, raw=None, script=None, role=None):
        import json
        import subprocess
        if with_roles:
            os.makedirs(os.path.join(project, ".claude", "roles"), exist_ok=True)
        env = dict(os.environ, CLAUDE_PROJECT_DIR=project)
        env.pop("RPV_ROLE", None)
        env.pop("ALPHA_ROLE", None)
        if role:
            env["RPV_ROLE"] = role
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


class EnvNames(unittest.TestCase):
    """Переменные `RPV_*`, запасные `ALPHA_*` (прежние названия): RPV_ главнее."""

    HERE = os.path.dirname(os.path.abspath(__file__))

    def read(self, env_extra):
        import subprocess
        env = {k: v for k, v in os.environ.items() if not k.startswith(("RPV_", "ALPHA_"))}
        env.update(env_extra)
        code = ("import delete_guard as d; print(d.STAGE, d.REMOTE_ROOTS, sorted(d.HOST_ROOTS), "
                "d.dispatcher_role(), bool(d.BOX_RE.search('host-9.example')))")
        r = subprocess.run([sys.executable, "-c", code], cwd=self.HERE, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_alpha_names_still_read(self):
        out = self.read({"ALPHA_GUARD_STAGE": "/dev/shm/s1", "ALPHA_GUARD_REMOTE_ROOTS": "/srv/a/", "ALPHA_ROLE": "judge",
                         "ALPHA_GUARD_HOST_ROOTS": "h1=/x/", "ALPHA_GUARD_FORBIDDEN_HOSTS": "host-9.example"})
        self.assertEqual(out, "/dev/shm/s1 ('/srv/a/',) ['h1'] judge True")

    def test_rpv_names_win(self):
        out = self.read({"RPV_GUARD_STAGE": "/dev/shm/new", "ALPHA_GUARD_STAGE": "/dev/shm/old",
                         "RPV_GUARD_REMOTE_ROOTS": "/srv/new/", "ALPHA_GUARD_REMOTE_ROOTS": "/srv/old/",
                         "RPV_ROLE": "engineer", "ALPHA_ROLE": "judge"})
        self.assertEqual(out, "/dev/shm/new ('/srv/new/',) [] engineer False")

    def test_texts_name_rpv_variables_only(self):
        for text in (dg.REASON, dg.REASON_OVERWRITE, dg.REASON_IRREVERSIBLE):
            self.assertNotIn("ALPHA_", text)
        self.assertIn("RPV_GUARD_REMOTE_ROOTS", dg.REASON)


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
            "ssh": ("Bash", {"command": "ssh deck@192.0.2.49 'rm -rf /home/other/x'"}),
            "tee-trunc-cp": ("Bash", {"command": f'echo a > "{self.f}"'}),
            "git": ("Bash", {"command": "git reset --hard"}),
            "heavy": ("Bash", {"command": "ssh root@203.0.113.3 'du -sh /data'"}),
            "file": ("Write", {"file_path": self.f}),
            "ceo-signal": ("Write", {"file_path": "C:/x/.claude/dispatcher/ceo-inbox.md"}),
            "legacy": ("Bash", {"command": "echo 'abc; rm -rf /c/Users/x"}),
        }
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
