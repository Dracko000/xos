import unittest
from shell.xos_shell import is_loopback, normalize_language, Shell

class MockCursesWindow:
    def __init__(self, h=30, w=100):
        self.h = h
        self.w = w
    def getmaxyx(self):
        return self.h, self.w
    def erase(self): pass
    def addstr(self, *args, **kwargs): pass
    def move(self, *args, **kwargs): pass
    def refresh(self): pass
    def keypad(self, val): pass
    def nodelay(self, val): pass
    def timeout(self, val): pass

class TestShellLogic(unittest.TestCase):
    def test_is_loopback(self):
        # Valid loopbacks
        self.assertTrue(is_loopback("http://127.0.0.1:11434"))
        self.assertTrue(is_loopback("http://localhost:11434"))
        self.assertTrue(is_loopback("http://[::1]:11434"))
        self.assertTrue(is_loopback("http://:11434")) # No host
        
        # Invalid loopbacks (remote)
        self.assertFalse(is_loopback("http://192.168.1.1:11434"))
        self.assertFalse(is_loopback("http://google.com"))
        self.assertFalse(is_loopback("https://api.ollama.com"))
        self.assertFalse(is_loopback("not-a-url"))

    def test_normalize_language(self):
        self.assertEqual(normalize_language("en"), "en")
        self.assertEqual(normalize_language("en_US"), "en")
        self.assertEqual(normalize_language("id-ID"), "id")
        self.assertEqual(normalize_language("fr"), None)
        self.assertEqual(normalize_language(""), None)

    def test_wrap_text(self):
        mock_scr = MockCursesWindow()
        shell = Shell(mock_scr, headless=True)
        
        text = "Hello world this is a long sentence that should wrap"
        # width 10 (inner width = 8)
        # "Hello" (5), "world" (5) -> "Hello" | "world"
        wrapped = shell._wrap(text, 10)
        self.assertTrue(len(wrapped) > 1)
        self.assertLessEqual(max(len(l) for l in wrapped), 10)

    def test_command_clear(self):
        mock_scr = MockCursesWindow()
        shell = Shell(mock_scr, headless=True)
        shell.messages = [{"role": "user", "text": "hi"}]
        shell._command("/clear")
        self.assertEqual(len(shell.messages), 0)
        self.assertEqual(shell.scroll, 0)

if __name__ == "__main__":
    unittest.main()
