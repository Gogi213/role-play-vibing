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
os.environ["ALPHA_GUARD_REMOTE_ROOTS"] = "~/alpha/,$home/alpha/,${home}/alpha/,/home/deck/alpha/,/opt/alpha-compute/"
os.environ["ALPHA_GUARD_STAGE"] = "/dev/shm/alpha-stage"
os.environ["ALPHA_GUARD_FORBIDDEN_HOSTS"] = "203.0.113.1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import delete_guard as dg  # noqa: E402

CWD = r"C:\visual projects\alpha"
SCRATCH = "C:/Users/3D6B~1/AppData/Local/Temp/claude/C--visual-projects-alpha/abc/scratchpad"


def denied(cmd, cwd=CWD):
    return dg.check(cmd, cwd) is not None


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
        self.no("cd /tmp && rm -rf data")

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
        cls.out = tempfile.mkdtemp(prefix="dg-outside-")          # вне корней: системный temp, не папка проекта
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


class HookEntryPoint(unittest.TestCase):
    """Точка входа PreToolUse: JSON со stdin → deny с причиной; не Bash / нет `.claude/roles` в проекте — тишина."""

    def run_hook(self, event, project, with_roles=True):
        import json
        import subprocess
        if with_roles:
            os.makedirs(os.path.join(project, ".claude", "roles"), exist_ok=True)
        env = dict(os.environ, CLAUDE_PROJECT_DIR=project)
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "delete_guard.py")],
                           input=json.dumps(event).encode("utf-8"), capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.decode("utf-8").strip()

    def test_deny_allow_and_silent_cases(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            ev = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /etc/x"}, "cwd": d}
            out = json.loads(self.run_hook(ev, d))["hookSpecificOutput"]
            self.assertEqual(out["permissionDecision"], "deny")
            self.assertIn("Удаление запрещено", out["permissionDecisionReason"])
            ok = dict(ev, tool_input={"command": "rm -rf " + d.replace("\\", "/") + "/data/tmp"})
            self.assertEqual(self.run_hook(ok, d), "")                       # внутри проекта — можно
            self.assertEqual(self.run_hook(dict(ev, tool_name="Read"), d), "")
        with tempfile.TemporaryDirectory() as d2:                           # проект без команды ролей — молчит
            ev = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /etc/x"}, "cwd": d2}
            self.assertEqual(self.run_hook(ev, d2, with_roles=False), "")


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


if __name__ == "__main__":
    unittest.main()
