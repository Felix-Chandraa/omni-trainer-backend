from pathlib import Path
import unittest

BUS=Path.home()/"Downloads"/"omni_flight-main"/"Feather-Flight-main"/"src"/"js"/"bus.js"

def flight_fn():
    t=BUS.read_text()
    marker="function sendFlightCommand(name, payload = {}) {"
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
    return t[start:end]

class Dev016FlightInputIntentTests(unittest.TestCase):
    def test_real_flight_path_records_intent(self):
        f=flight_fn()
        self.assertIn("DEV016_FLIGHT_INPUT_INTENT",f)
        self.assertIn('action:"command_intent"',f)
        self.assertIn('scope:"flight"',f)
        self.assertIn("client_mono_ns:mono",f)

    def test_intent_precedes_real_command(self):
        f=flight_fn()
        self.assertLess(
            f.index('action:"command_intent"'),
            f.index("socket.send(JSON.stringify(command))")
        )

    def test_intent_and_command_share_identity_fields(self):
        f=flight_fn()
        for token in (
            "command_id:id",
            "generation:flightAuthority.generation",
            "authority_epoch:flightAuthority.epoch",
            "sequence:flightCommandSeq",
            "expiry_ms:250",
        ):
            self.assertGreaterEqual(f.count(token),2,token)

if __name__=="__main__": unittest.main()
