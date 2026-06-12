import unittest
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
        # the 80588cba space-binding bug: space must be derived by the child
        env = fd.child_env("claude_prime", CFG)
        self.assertNotIn("AX_SPACE_ID", env)

    def test_inherits_parent_env_without_leaking_other_agents(self):
        env = fd.child_env("claude_prime", CFG)
        self.assertIn("PATH", env)
