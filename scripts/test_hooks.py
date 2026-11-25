#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests of the Git hooks and scripts/chain.sh, in scratch repositories.

Each test copies the hooks into a new repository, installs them with scripts/install-git-hooks and
makes real commits, so what is tested is what a contributor gets: the rules of the commit-msg hook
(the seven rules of https://cbea.ms/git-commit/), the same rules on a stored commit, the Change-Id
that a squash leaves several of, the advice after a rewrite, the guard of post-commit during a
rebase, the file names that pre-commit refuses (also where wc pads its count, as on macOS), what
prepare-commit-msg writes, the installer, and the check of the prerequisites.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS)
FILES = [
    "common.sh", "chain.sh", "commit-msg.hook", "commitlog.sh", "post-commit.hook", "post-rewrite.hook",
    "pre-commit.hook", "pre-push.hook", "prepare-commit-msg.hook", "install-git-hooks", "check-prereq.sh",
    "repo.sh", "aspell-pws",
]
GOOD = "Add a line to the file\n\nThe file gets one more line so that a test has a change to commit.\nThe line says what the change is for.\n"


class Repo:
    """A scratch repository with the hooks of this tree installed."""

    def __init__(self, install=True):
        self.dir = tempfile.mkdtemp(prefix="hooktest.")
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
                        GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_NOSYSTEM="1", HOME=self.dir)
        self.env.pop("CHAIN_BASE", None)
        self.git("init", "-q", ".")
        self.git("config", "commit.gpgsign", "false")
        os.makedirs(os.path.join(self.dir, "scripts"))
        os.makedirs(os.path.join(self.dir, "tools"))
        for name in FILES:
            shutil.copy(os.path.join(SCRIPTS, name), os.path.join(self.dir, "scripts", name))
        shutil.copy(os.path.join(ROOT, "tools", "hashchain.c"), os.path.join(self.dir, "tools", "hashchain.c"))
        self.write("f", "base\n")
        self.git("add", "-A")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.env["CHAIN_BASE"] = self.base
        if install:
            r = self.run("scripts/install-git-hooks")
            assert r.returncode == 0, r.stdout + r.stderr

    def run(self, *argv, **kw):
        return subprocess.run(argv, cwd=self.dir, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, **kw)

    def git(self, *args, **kw):
        return self.run("git", *args, **kw)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "a" if name == "f" and os.path.exists(os.path.join(self.dir, name)) else "w") as f:
            f.write(text)

    def commit(self, message, *flags, hooks=True):
        """Change the file and commit with `message` (hooks=False: with no hook at all)."""
        self.write("f", f"line {len(os.listdir(self.dir))} {self.git('rev-parse', 'HEAD').stdout[:7]}\n")
        self.git("add", "f")
        path = os.path.join(self.dir, ".msg")
        with open(path, "w") as f:
            f.write(message)
        off = [] if hooks else ["-c", "core.hooksPath=/dev/null"]
        return self.git(*off, "commit", "-q", *flags, "-F", path)

    def message(self, rev="HEAD"):
        return self.git("log", "-1", "--format=%B", rev).stdout

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class HookTest(unittest.TestCase):
    def repo(self, **kw):
        r = Repo(**kw)
        self.addCleanup(r.close)
        return r


class MessageRulesTest(HookTest):
    def test_a_good_message_is_stamped(self):
        r = self.repo()
        c = r.commit(GOOD)
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
        self.assertRegex(r.message(), r"\nChange-Id: I[0-9a-f]{40}\n")
        self.assertEqual(r.run("scripts/chain.sh", "verify").returncode, 0)

    def test_each_rule_refuses_its_message(self):
        subject = "Add a line to the file"
        body = "\n\nThe file gets one more line so that a test has a change to commit.\n"
        cases = [
            ("no blank line after the subject", f"{subject}\nThe file gets one more line right under it.\n", "Separate subject from body"),
            ("a subject over 50 characters", "Add a line to the file so that the test has something to commit" + body, "Limit the subject line"),
            ("a lowercase subject", "add a line to the file" + body, "Capitalize the subject line"),
            ("a period at the end", subject + "." + body, "Do not end the subject line with a period"),
            ("a body line over 72 characters", subject + "\n\n" + "The file gets one more line so that a test has a change to commit and this line is long.\n", "Wrap the body at 72"),
            ("parentheses in the subject", "Add a line (the second) to the file" + body, "parentheses"),
            ("a conventional commit subject", "feat: add a line to the file" + body, "conventional commit"),
            ("a single word", "Add" + body, "single-word"),
        ]
        for what, message, fragment in cases:
            with self.subTest(what):
                r = self.repo()
                c = r.commit(message)
                self.assertNotEqual(c.returncode, 0, what)
                self.assertIn(fragment, c.stdout + c.stderr)
                self.assertEqual(r.git("rev-parse", "HEAD").stdout.strip(), r.base, "nothing was committed")

    def test_the_stored_message_is_checked_with_the_same_rules(self):
        r = self.repo()
        self.assertEqual(r.commit(GOOD).returncode, 0)
        self.assertEqual(r.run("scripts/commit-msg.hook", "--stored", "HEAD").returncode, 0)
        # a commit that skipped the hooks, with a message that breaks rule 1
        c = r.commit("Add a line to the file\nNo blank line under the subject.\n", "--no-verify")
        self.assertEqual(c.returncode, 0)
        s = r.run("scripts/commit-msg.hook", "--stored", "HEAD")
        self.assertEqual(s.returncode, 1)
        self.assertIn("Separate subject from body", s.stdout)
        log = r.run("scripts/commitlog.sh")
        self.assertNotEqual(log.returncode, 0)
        self.assertIn("Breaks the message rules", log.stdout + log.stderr)

    def test_a_missing_commit_is_an_error(self):
        r = self.repo()
        self.assertEqual(r.run("scripts/commit-msg.hook", "--stored", "deadbeef").returncode, 2)


class ChangeIdTest(HookTest):
    def test_a_squash_message_with_several_change_ids_keeps_one(self):
        r = self.repo()
        two = GOOD + "\nChange-Id: I" + "1" * 40 + "\nChange-Id: I" + "2" * 40 + "\n"
        c = r.commit(two)
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
        self.assertEqual(len(re.findall(r"^Change-Id:", r.message(), re.M)), 1)
        self.assertIn("consolidated", c.stderr)
        self.assertEqual(r.run("scripts/chain.sh", "verify").returncode, 0)

    def test_verify_names_a_commit_with_several_ids_and_rechain_repairs_it(self):
        r = self.repo()
        self.assertEqual(r.commit(GOOD).returncode, 0)
        two = GOOD + "\nChange-Id: I" + "1" * 40 + "\nChange-Id: I" + "2" * 40 + "\n"
        self.assertEqual(r.commit(two, hooks=False).returncode, 0)  # what a squash made without the hooks leaves
        v = r.run("scripts/chain.sh", "verify")
        self.assertNotEqual(v.returncode, 0)
        self.assertIn("2 Change-Id trailers", v.stdout + v.stderr)
        fix = r.run("scripts/chain.sh", "rechain")
        self.assertEqual(fix.returncode, 0, fix.stdout + fix.stderr)
        self.assertEqual(len(re.findall(r"^Change-Id:", r.message(), re.M)), 1)
        self.assertEqual(r.run("scripts/chain.sh", "verify").returncode, 0)

    def test_post_rewrite_advises_and_never_fails(self):
        r = self.repo()
        self.assertEqual(r.commit(GOOD).returncode, 0)
        quiet = r.run(".git/hooks/post-rewrite", "amend")
        self.assertEqual((quiet.returncode, quiet.stderr), (0, ""))
        self.assertEqual(r.commit(GOOD, "--no-verify").returncode, 0)  # no Change-Id: the chain breaks
        advice = r.run(".git/hooks/post-rewrite", "rebase")
        self.assertEqual(advice.returncode, 0)
        self.assertIn("scripts/chain.sh rechain", advice.stderr)

    def test_post_commit_leaves_the_chain_alone_during_a_rebase(self):
        r = self.repo()
        self.assertEqual(r.commit(GOOD).returncode, 0)
        # a commit with a Change-Id that is not the link of the chain, made with no hook at all
        wrong = GOOD + "\nChange-Id: I" + "a" * 40 + "\n"
        self.assertEqual(r.commit(wrong, hooks=False).returncode, 0)
        broken = r.git("rev-parse", "HEAD").stdout.strip()
        os.makedirs(os.path.join(r.dir, ".git", "rebase-merge"))
        self.assertEqual(r.run(".git/hooks/post-commit").returncode, 0)
        self.assertEqual(r.git("rev-parse", "HEAD").stdout.strip(), broken, "a rebase's commits are not amended")
        shutil.rmtree(os.path.join(r.dir, ".git", "rebase-merge"))
        self.assertEqual(r.run(".git/hooks/post-commit").returncode, 0)
        self.assertNotEqual(r.git("rev-parse", "HEAD").stdout.strip(), broken, "outside a rebase it repairs HEAD")
        self.assertEqual(r.run("scripts/chain.sh", "verify").returncode, 0)


class BinaryFileTest(HookTest):
    def commit_binary(self, r, name):
        os.makedirs(os.path.dirname(os.path.join(r.dir, name)) or r.dir, exist_ok=True)
        with open(os.path.join(r.dir, name), "wb") as f:
            f.write(bytes(range(256)))
        r.git("add", name)
        return r.commit(GOOD)

    def test_an_undeclared_binary_file_is_refused(self):
        r = self.repo()
        p = self.commit_binary(r, "blob.bin")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not declared in .gitattributes", p.stdout + p.stderr)

    def test_an_empty_file_is_not_binary(self):
        r = self.repo()
        r.write("empty.bin", "")
        r.git("add", "empty.bin")
        p = r.commit(GOOD)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_binary_file_that_gitattributes_declares_is_committed(self):
        r = self.repo()
        r.write(".gitattributes", "/doc/*.png binary\n")
        r.git("add", ".gitattributes")
        p = self.commit_binary(r, "doc/a.png")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class FilenameTest(HookTest):
    NAME = "報告.txt"  # a file name in Chinese

    def repo_with(self, padded):
        """A repository whose hooks see the count of wc padded, as the wc of macOS prints it, or not."""
        r = self.repo()
        if padded:
            bin_dir = tempfile.mkdtemp(prefix="hooktest.bin.")
            self.addCleanup(shutil.rmtree, bin_dir, True)
            wc = os.path.join(bin_dir, "wc")
            with open(wc, "w") as f:
                f.write('#!/bin/sh\nout=$("%s" "$@") || exit\nprintf "%%8s\\n" "$(echo $out)"\n' % shutil.which("wc"))
            os.chmod(wc, 0o755)
            r.env["PATH"] = bin_dir + os.pathsep + r.env["PATH"]
        return r

    def test_a_non_ascii_name_is_refused_when_added_or_renamed(self):
        for padded in (False, True):
            for how in ("added", "renamed"):
                with self.subTest(how=how, padded=padded):
                    r = self.repo_with(padded)
                    if how == "added":
                        r.write(self.NAME, "notes\n")
                        r.git("add", self.NAME)
                    else:
                        r.write("report.txt", "notes\n")
                        r.git("add", "report.txt")
                        self.assertEqual(r.commit(GOOD).returncode, 0)
                        r.git("mv", "report.txt", self.NAME)  # staged as a rename, not as an addition
                    head = r.git("rev-parse", "HEAD").stdout
                    c = r.commit(GOOD)
                    self.assertNotEqual(c.returncode, 0)
                    self.assertIn("Non-ASCII filename detected", c.stdout + c.stderr)
                    self.assertEqual(r.git("rev-parse", "HEAD").stdout, head, "nothing was committed")

    def test_ascii_names_pass_also_with_a_padded_count(self):
        for padded in (False, True):
            with self.subTest(padded=padded):
                r = self.repo_with(padded)
                r.write("report.txt", "notes\n")
                r.git("add", "report.txt")
                c = r.commit(GOOD)
                self.assertEqual(c.returncode, 0, c.stdout + c.stderr)
                r.git("mv", "report.txt", "summary.txt")
                c = r.commit(GOOD)
                self.assertEqual(c.returncode, 0, c.stdout + c.stderr)

    def test_a_tracked_non_ascii_name_can_still_be_edited(self):
        for padded in (False, True):
            with self.subTest(padded=padded):
                r = self.repo_with(padded)
                r.write(self.NAME, "notes\n")
                r.git("add", self.NAME)
                self.assertEqual(r.commit(GOOD, hooks=False).returncode, 0)  # a name already in the tree
                self.assertEqual(r.run("scripts/chain.sh", "rechain").returncode, 0)
                r.write(self.NAME, "more notes\n")
                r.git("add", self.NAME)
                c = r.commit(GOOD)
                self.assertEqual(c.returncode, 0, c.stdout + c.stderr)


class PrepareCommitMsgTest(HookTest):
    def test_only_an_empty_new_message_gets_the_rules(self):
        r = self.repo()
        path = os.path.join(r.dir, "m")
        for source, text, rules in ((None, "", True), ("message", "", False), ("squash", "", False), ("merge", "", False),
                                    (None, "Already written\n", False)):
            with open(path, "w") as f:
                f.write(text)
            argv = ["scripts/../.git/hooks/prepare-commit-msg", path] + ([source] if source else [])
            hook = r.run(*argv)
            self.assertEqual(hook.returncode, 0, hook.stderr)
            with open(path) as f:
                got = f.read()
            self.assertEqual("Seven Rules" in got, rules, (source, text))
            if not rules:
                self.assertEqual(got, text)


class InstallerTest(HookTest):
    HOOKS = ["pre-commit", "commit-msg", "post-commit", "pre-push", "prepare-commit-msg", "post-rewrite"]

    def test_install_check_uninstall(self):
        r = self.repo(install=False)
        self.assertNotEqual(r.run("scripts/install-git-hooks", "--check").returncode, 0)
        self.assertEqual(r.run("scripts/install-git-hooks").returncode, 0)
        for h in self.HOOKS:
            self.assertTrue(os.path.islink(os.path.join(r.dir, ".git", "hooks", h)), h)
        self.assertEqual(r.run("scripts/install-git-hooks").returncode, 0, "twice is the same")
        self.assertEqual(r.run("scripts/install-git-hooks", "--check").returncode, 0)
        self.assertEqual(r.run("scripts/install-git-hooks", "--uninstall").returncode, 0)
        for h in self.HOOKS:
            self.assertFalse(os.path.lexists(os.path.join(r.dir, ".git", "hooks", h)), h)

    def test_a_hook_that_is_not_ours_is_left_alone(self):
        r = self.repo(install=False)
        mine = os.path.join(r.dir, ".git", "hooks", "pre-commit")
        with open(mine, "w") as f:
            f.write("#!/bin/sh\necho mine\n")
        os.chmod(mine, 0o755)
        i = r.run("scripts/install-git-hooks")
        self.assertNotEqual(i.returncode, 0)
        self.assertIn("not ours", i.stdout)
        self.assertIn("pre-commit.local", i.stdout)
        with open(mine) as f:
            self.assertIn("echo mine", f.read())
        self.assertFalse(os.path.islink(mine))
        r.run("scripts/install-git-hooks", "--uninstall")
        self.assertTrue(os.path.exists(mine), "uninstall removes only our links")

    def test_a_hooks_path_is_reported(self):
        r = self.repo(install=False)
        r.git("config", "core.hooksPath", "elsewhere")
        i = r.run("scripts/install-git-hooks")
        self.assertNotEqual(i.returncode, 0)
        self.assertIn("core.hooksPath", i.stdout)


class PrerequisiteTest(HookTest):
    def test_the_tools_of_this_machine_pass(self):
        r = self.repo(install=False)
        p = r.run("scripts/check-prereq.sh")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("bash", p.stdout)

    def test_a_missing_compiler_is_named_with_its_fix(self):
        r = self.repo(install=False)
        r.env["CC"] = "no-such-compiler-for-the-test"
        p = r.run("scripts/check-prereq.sh")
        self.assertEqual(p.returncode, 1)
        self.assertIn("install a C compiler", p.stdout)
        # and the installer refuses to go on without it
        self.assertNotEqual(r.run("scripts/install-git-hooks").returncode, 0)


if __name__ == "__main__":
    unittest.main()
