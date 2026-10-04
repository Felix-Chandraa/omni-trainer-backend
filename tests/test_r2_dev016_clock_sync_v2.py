from pathlib import Path
import unittest

BUS=Path.home()/"Downloads"/"omni_flight-main"/"Feather-Flight-main"/"src"/"js"/"bus.js"

class Dev016ClockSyncV2Tests(unittest.TestCase):
    def test_clock_has_single_pong_to_sample_owner(self):
        t=BUS.read_text()
        self.assertIn("DEV016_CLOCK_SYNC_V2",t)
        self.assertIn("DEV016_CLOCK_SINGLE_OWNER",t)
        self.assertIn('type: "clock_ping"',t)
        self.assertIn('if (msg.type === "clock_pong")',t)
        self.assertTrue('type:"clock_sample"' in t or 'type: "clock_sample"' in t)

        marker="function omniClockMessageListener(event) {"
        start=t.index(marker)
        brace=start + len(marker) - 1
        depth=0; end=None
        for i in range(brace,len(t)):
            if t[i]=="{": depth+=1
            elif t[i]=="}":
                depth-=1
                if depth==0:
                    end=i+1
                    break
        listener=t[start:end]
        self.assertNotIn('type:"clock_sample"',listener)
        self.assertNotIn('type: "clock_sample"',listener)

    def test_periodic_ping_refresh(self):
        t=BUS.read_text()
        self.assertIn("setInterval(omniSendClockPing, 2000)",t)
        self.assertIn('addEventListener("close", omniStopClockSync)',t)

    def test_evidence_hooks_remain(self):
        t=BUS.read_text()
        self.assertIn("DEV016_RECEIVED_STATE_ACK",t)
        self.assertIn("DEV016_COMMAND_INTENT_SEND",t)

if __name__=="__main__": unittest.main()
