import json, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
import prospector as P

SEG = {"id": "clinics_ht", "label": "clinique / cabinet médical", "country": "Haïti"}


class Phone(unittest.TestCase):
    def test_cases(self):
        n = P.normalize_phone
        self.assertEqual(n("47 30 2131"), "+50947302131")
        self.assertEqual(n("+509 3712 3456"), "+50937123456")
        self.assertEqual(n("509-3712-3456"), "+50937123456")
        self.assertEqual(n("+1 305 555 0123; +509 3333 4444"), "+13055550123")
        self.assertEqual(n("00509 3712 3456"), "+50937123456")
        self.assertEqual(n("123"), "")
        self.assertEqual(n(None), "")


class Osm(unittest.TestCase):
    def test_parse(self):
        data = {"elements": [
            {"type": "node", "id": 1, "tags": {"name": "Clinique A", "phone": "3712 3456", "addr:city": "Pétion-Ville"}},
            {"type": "way", "id": 2, "tags": {"name": "Cabinet B", "email": "b@cab.ht", "website": "https://cab.ht"}},
            {"type": "node", "id": 3, "tags": {"name": "Sans contact"}},
            {"type": "node", "id": 4, "tags": {"phone": "37123456"}},
        ]}
        r = P.parse_osm(data, SEG)
        self.assertEqual(len(r), 2)
        self.assertEqual(r[0]["contact"], "+50937123456")
        self.assertEqual(r[0]["channel"], "whatsapp")
        self.assertEqual(r[0]["score"], 75)  # 60 + 15 (sans site)
        self.assertEqual(r[1]["channel"], "email")
        self.assertEqual(r[1]["score"], 60)

    def test_query(self):
        q = P.build_overpass_query(["amenity=clinic"])
        self.assertIn("17.9,-74.6,20.2,-71.5", q)
        self.assertIn('["amenity"="clinic"]', q)


class Extract(unittest.TestCase):
    def test_json(self):
        self.assertEqual(P.extract_json_array('ok [{"name":"X"}] fin'), [{"name": "X"}])
        self.assertEqual(P.extract_json_array("rien"), [])
        self.assertEqual(P.extract_json_array("[oops"), [])

    def test_verify_blocks_invented(self):
        corpus = "Contact: info@asso-ht.org ou +1 305 555 0123"
        ok = P.verify_candidate({"name": "A", "contact": "info@asso-ht.org"}, corpus)
        self.assertEqual(ok["channel"], "email")
        self.assertIsNone(P.verify_candidate({"name": "A", "contact": "faux@invente.org"}, corpus))
        self.assertIsNone(P.verify_candidate({"name": "A", "contact": "+509 1111 2222"}, corpus))
        ph = P.verify_candidate({"name": "A", "contact": "305 555 0123"}, corpus)
        self.assertEqual(ph["contact"], "+13055550123")

    def test_dedupe(self):
        a = {"name": "X", "contact": "a@b.c"}
        self.assertEqual(len(P.dedupe([a, dict(a, name="x")])), 1)


class Push(unittest.TestCase):
    def test_push(self):
        seen = {}

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["auth"] = self.headers.get("Authorization")
                seen["path"] = self.path
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.setdefault("n", []).append(len(body["prospects"]))
                out = json.dumps({"ok": True, "added": len(body["prospects"])}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers()
                self.wfile.write(out)
            def log_message(self, *a): pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        items = [{"name": f"P{i}", "contact": f"{i}@x.ht"} for i in range(120)]
        added = P.push(items, f"http://127.0.0.1:{srv.server_port}", "tok")
        srv.shutdown()
        self.assertEqual(added, 120)
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertEqual(seen["path"], "/api/prospects")
        self.assertEqual(seen["n"], [50, 50, 20])


class Rotation(unittest.TestCase):
    def test_pick(self):
        import datetime
        segs = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        self.assertEqual(P.pick_segment(segs, "b")["id"], "b")
        self.assertIn(P.pick_segment(segs, None, datetime.date(2026, 11, 12))["id"], "abc")


class Fallback(unittest.TestCase):
    def test_osm_down_uses_web(self):
        import argparse
        orig = (P.fetch_osm, P.fetch_web)
        P.fetch_osm = lambda f: (_ for _ in ()).throw(RuntimeError("down"))
        P.fetch_web = lambda seg, key, rng, mq=2: [
            {"name": "Clinique Web", "contact": "+50937123456", "segment": "x", "country": "Haïti",
             "channel": "whatsapp", "source_url": "", "score": 70, "notes": "n"}]
        try:
            args = argparse.Namespace(segment="clinics_ht", max=5, dry_run=True)
            self.assertEqual(P.run(args, env={"TAVILY_API_KEY": "k"}), 0)
            with self.assertRaises(Exception):
                P.run(args, env={})
        finally:
            P.fetch_osm, P.fetch_web = orig


if __name__ == "__main__":
    unittest.main()
