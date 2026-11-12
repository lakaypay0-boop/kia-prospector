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


class PhonesAbroad(unittest.TestCase):
    def test_france_et_numeros_verts(self):
        self.assertEqual(P.normalize_phone("07 45 17 48 65", "33"), "+33745174865")
        self.assertEqual(P.normalize_phone("+33 1 23 45 67 89", "33"), "+33123456789")
        self.assertEqual(P.phone_channel("+33745174865"), "whatsapp")
        self.assertEqual(P.phone_channel("+33123456789"), "téléphone")
        self.assertTrue(P.is_toll_free("+18003540365"))
        self.assertFalse(P.is_toll_free("+13055734871"))
        c = {"name": "Assoc", "contact": "07 45 17 48 65", "country": "France"}
        self.assertEqual(P.verify_candidate(c, "tel 07 45 17 48 65")["contact"], "+33745174865")
        v = {"name": "Charities", "contact": "1-800-354-0365", "country": "États-Unis"}
        self.assertIsNone(P.verify_candidate(v, "call 1-800-354-0365"))

    def test_contact_partage_entre_organisations(self):
        a = {"name": "UNHCR", "contact": "x@y.org", "channel": "email"}
        b = {"name": "UN Women", "contact": "x@y.org", "channel": "email"}
        c = {"name": "Autre", "contact": "z@y.org", "channel": "email"}
        self.assertEqual([p["name"] for p in P.dedupe([a, b, c])], ["Autre"])


class Exclusions(unittest.TestCase):
    SEG = {"countries": ["Haïti", "États-Unis"]}

    def test_exclusions(self):
        ok = {"name": "Fondation X", "notes": "fondation, Port-au-Prince", "contact": "a@fx.ht", "country": "Haïti"}
        self.assertFalse(P.excluded(ok, self.SEG))
        self.assertTrue(P.excluded(dict(ok, country="Éthiopie"), self.SEG))
        self.assertTrue(P.excluded(dict(ok, name="Banque de l'Union Haïtienne"), self.SEG))
        self.assertTrue(P.excluded(dict(ok, name="Mission Haiti", notes="mission de l'OEA"), self.SEG))
        self.assertTrue(P.excluded(dict(ok, contact="x@diplomatie.ht"), self.SEG))
        self.assertTrue(P.excluded(dict(ok, name="Société Financière Haïtienne"), self.SEG))
        self.assertFalse(P.excluded(dict(ok, country="etats-unis"), self.SEG))
        self.assertFalse(P.excluded(ok, {}))


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
        a = {"name": "X", "contact": "a@b.c", "channel": "email"}
        self.assertEqual(len(P.dedupe([a, dict(a, name="x")])), 1)
        w = {"name": "WinWell Dental Spa", "contact": "+50929403368", "channel": "téléphone"}
        e = {"name": "WinWell  Dental Spa", "contact": "i@w.com", "channel": "email"}
        self.assertEqual(P.dedupe([w, e]), [e])

    def test_channel(self):
        self.assertEqual(P.phone_channel("+50929403368"), "téléphone")
        self.assertEqual(P.phone_channel("+50937123456"), "whatsapp")
        self.assertEqual(P.phone_channel("+13055550123"), "whatsapp")


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


class AnnuairesEtNomsVides(unittest.TestCase):
    SEG = {"countries": ["États-Unis", "Canada"]}
    OK = {"name": "Rdjess Traiteur", "notes": "traiteur, Montréal", "contact": "+15145764563",
          "country": "Canada", "source_url": "https://rdjess.com"}

    def test_exclusions(self):
        self.assertFalse(P.excluded(self.OK, self.SEG))
        self.assertTrue(P.excluded(dict(self.OK, name="Restaurant Haïtien (nom non spécifié)"), self.SEG))
        self.assertTrue(P.excluded(dict(self.OK, source_url="https://www.yelp.com/search?find_desc=Haitian"), self.SEG))
        self.assertTrue(P.excluded(dict(self.OK, source_url="https://traiteursmontreal.com/haitien"), self.SEG))
        self.assertFalse(P.excluded(dict(self.OK, source_url="https://www.facebook.com/chezmarie"), self.SEG))
        self.assertFalse(P.excluded(dict(self.OK, source_url="https://mabbbakery.com"), self.SEG))

class DiasporaBiz(unittest.TestCase):
    def test_segment_commerces(self):
        import os
        segs = json.load(open(os.path.join(P.HERE, "segments.json"), encoding="utf-8"))
        seg = next(s for s in segs if s["id"] == "diaspora_biz")
        self.assertIn("commerce", seg["label"])
        self.assertIn("restaurants", seg["focus"])
        self.assertGreaterEqual(len(seg["queries"]), 12)
        blob = " ".join(seg["queries"]).lower()
        for mot in ("restaurant", "bakery", "boulangerie", "traiteur", "salon", "caterer"):
            self.assertIn(mot, blob)

    def test_focus_envoye_au_llm(self):
        import random
        seen = {}
        orig = (P.tavily_search, P.llm_complete)
        P.tavily_search = lambda q, key, n=10: [{"url": "u", "title": "t", "content": "Chez Marie, info@chezmarie.com"}]
        def fake(system, user, env=None):
            seen["system"] = system
            return '[{"name":"Chez Marie","contact":"info@chezmarie.com","country":"États-Unis","notes":"restaurant, Miami","source_url":"u"}]'
        P.llm_complete = fake
        try:
            seg = {"id": "diaspora_biz", "label": "commerce diaspora", "country": "Diaspora",
                   "queries": ["q"], "focus": "CIBLE UNIQUEMENT des restaurants.", "countries": ["États-Unis"]}
            out = P.fetch_web(seg, "k", random.Random(1), 1)
        finally:
            P.tavily_search, P.llm_complete = orig
        self.assertIn("CIBLE UNIQUEMENT des restaurants.", seen["system"])
        self.assertTrue(seen["system"].startswith(P.EXTRACT_SYS))
        self.assertEqual(out[0]["name"], "Chez Marie")

if __name__ == "__main__":
    unittest.main()
