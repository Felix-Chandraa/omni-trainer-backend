from pathlib import Path
import unittest
BUS=Path.home()/"Downloads"/"omni_flight-main"/"Feather-Flight-main"/"src"/"js"/"bus.js"
class Dev016ClockSingleOwnerTests(unittest.TestCase):
    def test_dev016_listener_is_ping_only(self):
        t=BUS.read_text()
        start=t.index("function omniClockMessageListener(event) {")
        brace=t.index("{",start)
        depth=0
        end=None
        for i in range(brace,len(t)):
            if t[i]=="{": depth+=1
            elif t[i]=="}":
                depth-=1
                if depth==0:
                    end=i+1
                    break
        listener=t[start:end]
        self.assertIn("DEV016_CLOCK_SINGLE_OWNER",listener)
        self.assertNotIn('type: "clock_sample"',listener)
        self.assertIn('msg.type === "welcome"',listener)
    def test_ping_refresh_remains(self):
        t=BUS.read_text()
        self.assertIn('type: "clock_ping"',t)
        self.assertIn("setInterval(omniSendClockPing, 2000)",t)
    def test_existing_clock_sample_handler_remains(self):
        t=BUS.read_text()
        self.assertIn("clock_sample",t)
    def test_evidence_hooks_preserved(self):
        t=BUS.read_text()
        self.assertIn("DEV016_RECEIVED_STATE_ACK",t)
        self.assertIn("DEV016_COMMAND_INTENT_SEND",t)
if __name__=="__main__": unittest.main()
