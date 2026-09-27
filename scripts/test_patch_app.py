"""Signing regression tests; no signing certificate or macOS tools required."""
import subprocess
import plistlib
import unittest
from pathlib import Path
from unittest import mock

import patch_app


class SigningPrivacyTests(unittest.TestCase):
    def test_codesign_output_is_captured(self):
        with mock.patch.object(patch_app.subprocess, "run") as run:
            patch_app.run(["codesign", "--sign", "synthetic-selector", "fixture"])
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_signing_failure_does_not_echo_selector_or_diagnostics(self):
        command = ["codesign", "--sign", "synthetic-selector", "fixture"]
        failure = subprocess.CalledProcessError(
            1, command, output=b"synthetic-fingerprint", stderr=b"synthetic-team"
        )
        with mock.patch.object(patch_app.subprocess, "run", side_effect=failure), \
             self.assertRaises(RuntimeError) as caught:
            patch_app.run(command)
        for value in ("synthetic-selector", "synthetic-fingerprint", "synthetic-team"):
            self.assertNotIn(value, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_other_build_commands_keep_existing_behavior(self):
        with mock.patch.object(patch_app.subprocess, "run") as run:
            patch_app.run(["go", "build"])
        self.assertNotIn("capture_output", run.call_args.kwargs)


class IndependentBundleTests(unittest.TestCase):
    def test_sanitization_removes_inherited_grants_but_keeps_runtime(self):
        original = {
            "com.apple.application-identifier": "TESTTEAM01.example.app",
            "com.apple.developer.team-identifier": "TESTTEAM01",
            "com.apple.security.application-groups": ["TESTTEAM01.group"],
            "keychain-access-groups": ["TESTTEAM01.shared"],
            "com.apple.developer.aps-environment": "production",
            "com.apple.security.cs.allow-jit": True,
            "com.apple.security.cs.allow-unsigned-executable-memory": True,
            "com.apple.security.network.client": True,
        }
        output = subprocess.CompletedProcess(
            args=["codesign"], returncode=0, stdout=plistlib.dumps(original), stderr=b""
        )
        with mock.patch.object(patch_app.subprocess, "run", return_value=output):
            result = patch_app.sanitized_runtime_entitlements(Path("copied-executable"))
        self.assertEqual(result, {
            "com.apple.security.cs.allow-jit": True,
            "com.apple.security.cs.allow-unsigned-executable-memory": True,
            "com.apple.security.network.client": True,
        })
        self.assertEqual(original["com.apple.developer.aps-environment"], "production")


class SigningTeamTests(unittest.TestCase):
    def resolve(self, identity, metadata=("true", "TEAMID5678")):
        with mock.patch.object(patch_app.shutil, "copyfile") as copy, \
             mock.patch.object(patch_app, "run") as run, \
             mock.patch.object(patch_app, "signed_code_metadata", return_value=metadata):
            result = patch_app.signing_team_identifier(identity)
        return result, copy, run

    def test_development_name_suffix_is_not_the_team(self):
        result, copy, run = self.resolve("Apple Development: Example (PERSON1234)")
        self.assertEqual(result, "TEAMID5678")
        self.assertEqual(str(copy.call_args.args[0]), "/usr/bin/true")
        self.assertIn("Apple Development: Example (PERSON1234)", run.call_args.args[0])

    def test_certificate_fingerprint_selector_is_preserved(self):
        fingerprint = "A" * 40
        result, _, run = self.resolve(fingerprint)
        self.assertEqual(result, "TEAMID5678")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--sign") + 1], fingerprint)

    def test_developer_id_uses_actual_signature_team(self):
        result, _, _ = self.resolve("Developer ID Application: Example (TEAMID5678)")
        self.assertEqual(result, "TEAMID5678")

    def test_adhoc_does_not_invoke_signing_tools(self):
        with mock.patch.object(patch_app, "run") as run, \
             mock.patch.object(patch_app.shutil, "copyfile") as copy:
            self.assertIsNone(patch_app.signing_team_identifier("-"))
        run.assert_not_called()
        copy.assert_not_called()

    def test_missing_or_invalid_team_fails_closed(self):
        for team in (None, "not set", "short", "TEAMID5678\nother"):
            with self.subTest(team=team), self.assertRaises(RuntimeError):
                self.resolve("Apple Development: Example (PERSON1234)", ("true", team))

    def test_signing_failure_is_not_replaced_by_name_suffix(self):
        with mock.patch.object(patch_app.shutil, "copyfile"), \
             mock.patch.object(patch_app, "run", side_effect=subprocess.CalledProcessError(1, "codesign")), \
             self.assertRaises(subprocess.CalledProcessError):
            patch_app.signing_team_identifier("Apple Development: Example (PERSON1234)")


if __name__ == "__main__":
    unittest.main()
