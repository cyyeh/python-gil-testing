"""The library-comparison skill must be reachable by any coding agent, not just Claude Code."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL = ROOT / ".agents" / "skills" / "gil-lib-compare" / "SKILL.md"
CLAUDE_COPY = ROOT / ".claude" / "skills" / "gil-lib-compare" / "SKILL.md"


class SkillLocationsTest(unittest.TestCase):
    def test_canonical_skill_exists_in_cross_runtime_location(self):
        self.assertTrue(CANONICAL.exists(), CANONICAL)

    def test_claude_copy_is_byte_identical_to_canonical(self):
        self.assertEqual(CLAUDE_COPY.read_bytes(), CANONICAL.read_bytes(),
                         "run: cp .agents/skills/gil-lib-compare/SKILL.md .claude/skills/gil-lib-compare/SKILL.md")

    def test_skill_frontmatter_has_name_and_description(self):
        text = CANONICAL.read_text()
        m = re.match(r"---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(m)
        self.assertIn("name: gil-lib-compare", m.group(1))
        self.assertIn("description:", m.group(1))
        self.assertLessEqual(len(m.group(0)), 1024)

    def test_skill_body_is_agent_neutral(self):
        self.assertNotIn("Claude", CANONICAL.read_text().split("---", 2)[2])


class AgentInstructionFilesTest(unittest.TestCase):
    def test_agents_md_points_every_agent_at_the_skill(self):
        text = (ROOT / "AGENTS.md").read_text()
        self.assertIn(".agents/skills/gil-lib-compare/SKILL.md", text)
        self.assertIn("check_gil_support.py", text)
        self.assertIn("compare_lib", text)

    def test_claude_and_gemini_context_files_import_agents_md(self):
        for name in ("CLAUDE.md", "GEMINI.md"):
            self.assertIn("@AGENTS.md", (ROOT / name).read_text(), name)


if __name__ == "__main__":
    unittest.main()
