import os
import unittest
from unittest import mock

import fleet_daemon as fd

CFG = {"fleet": {"device": "laptop", "sponsor": "@madtank"},
       "agents": {"claude_prime": {"token_file": "/abs/tok.json",
                                   "platform": "ax", "catchup": "ask"}}}

class ChildEnvTest(unittest.TestCase):
    def test_identity_env_set(self):
        env = fd.child_env("claude_prime", CFG)
        self.assertEqual(env["AX_AGENT_HANDLE"], "claude_prime")
        self.assertEqual(env["AX_TOKEN_FILE"], "/abs/tok.json")
        self.assertEqual(env["AX_SPONSOR"], "@madtank")

    def test_never_sets_space_id(self):
        # the 80588cba space-binding bug: space must be derived by the child.
        # Seed AX_SPACE_ID in the parent env so the strip is actually
        # exercised (without this, the test passes vacuously).
        with mock.patch.dict(os.environ, {"AX_SPACE_ID": "stale"}):
            env = fd.child_env("claude_prime", CFG)
        self.assertNotIn("AX_SPACE_ID", env)

    def test_strips_inherited_per_agent_vars(self):
        # same bug class as 80588cba through adjacent vars: a stale
        # AX_AGENT_ID (or per-agent file path) in the daemon's own env must
        # not leak into every child — the listener binds identity, self-echo
        # guards, and heartbeat/activity files from these.
        stale = {
            "AX_AGENT_ID": "stale-uuid",
            "AX_HEARTBEAT_FILE": "/stale/heartbeat",
            "AX_ACTIVITY_FILE": "/stale/activity",
            "AX_ACTIVITY_JSON_FILE": "/stale/activity.json",
            "AX_REMINDERS_FILE": "/stale/reminders.json",
            "AX_HOME_FEED_FILE": "/stale/home-feed.json",
            "AX_BUSY_MESSAGES_FILE": "/stale/busy-messages.json",
        }
        with mock.patch.dict(os.environ, stale):
            env = fd.child_env("claude_prime", CFG)
        for var in stale:
            self.assertNotIn(var, env)
        # per-agent identity is still injected from config, not inherited
        self.assertEqual(env["AX_AGENT_HANDLE"], "claude_prime")
        self.assertEqual(env["AX_TOKEN_FILE"], "/abs/tok.json")

    def test_stale_identity_vars_overwritten_not_inherited(self):
        with mock.patch.dict(os.environ, {"AX_AGENT_HANDLE": "stale_agent",
                                          "AX_TOKEN_FILE": "/stale/tok.json"}):
            env = fd.child_env("claude_prime", CFG)
        self.assertEqual(env["AX_AGENT_HANDLE"], "claude_prime")
        self.assertEqual(env["AX_TOKEN_FILE"], "/abs/tok.json")

    def test_inherits_parent_env_without_leaking_other_agents(self):
        env = fd.child_env("claude_prime", CFG)
        self.assertIn("PATH", env)
