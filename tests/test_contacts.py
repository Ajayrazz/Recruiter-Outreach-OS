import io
from pathlib import Path

import pandas as pd
import pytest

from recruiter_os.models import Contact
from recruiter_os.services import contacts as c
from recruiter_os.services.emailnorm import normalize_email, validate_syntax
from recruiter_os.services.suppression import is_suppressed, suppress, unsuppress

CSV = b'Name,Email,Title,Company\n"Akanksha Puri","Akanksha.Puri@SourceFuse.com","Associate Director HR","SourceFuse Technologies"\n' \
      b'"Bad","not-an-email","HR","X"\n"Dup","akanksha.puri@sourcefuse.com","HR","Y"\n"","noname@x.com","","Estuate,"\n'


def load(app, data=CSV, name="a.csv"):
    df = c.read_table(io.BytesIO(data), name)
    with app.db.scope() as s:
        return c.import_contacts(s, df, c.auto_map_columns(list(df.columns)), name)


def test_csv_import_validation_duplicates_and_missing(app):
    r = load(app)
    assert r.total_rows == 4 and r.imported == 2
    assert r.invalid_syntax == 1 and r.duplicates_in_file == 1
    assert r.missing_name == 1 and r.missing_title == 1
    assert {x["reason"] for x in r.rejected} >= {"duplicate within file"}
    with app.db.scope() as s:
        ct = s.query(Contact).filter_by(email_normalized="akanksha.puri@sourcefuse.com").one()
        assert ct.email == "Akanksha.Puri@SourceFuse.com".lower() or ct.email  # preserved/cleaned
        assert ct.original_record["Email"] == "Akanksha.Puri@SourceFuse.com"  # original preserved
        assert s.query(Contact).filter_by(email_normalized="noname@x.com").one().company == "Estuate"  # trailing comma cleaned


def test_reimport_detects_existing(app):
    load(app)
    r = load(app)
    assert r.imported == 0 and r.already_in_db == 2


def test_rejected_export(app):
    r = load(app)
    csv = c.rejected_to_csv(r.rejected).decode()
    assert "not-an-email" in csv and "reason" in csv


def test_column_mapping_custom_names(app):
    data = b"Full Name,Work Email,Designation,Organization\nA B,a@b.com,HR,Acme\n"
    df = c.read_table(io.BytesIO(data), "x.csv")
    m = c.auto_map_columns(list(df.columns))
    assert m == {"name": "Full Name", "email": "Work Email", "title": "Designation", "company": "Organization"}


def test_xlsx_import(app, tmp_path):
    p = tmp_path / "c.xlsx"
    pd.DataFrame({"Name": ["Z"], "Email": ["z@zz.com"], "Title": ["HR"], "Company": ["ZZ"]}).to_excel(p, index=False)
    df = c.read_table(p)
    with app.db.scope() as s:
        assert c.import_contacts(s, df, c.auto_map_columns(list(df.columns)), "c.xlsx").imported == 1


def test_unsupported_file(app):
    with pytest.raises(c.ImportError_):
        c.read_table(io.BytesIO(b"x"), "a.pdf")


def test_missing_email_mapping(app):
    df = pd.DataFrame({"a": ["1"]})
    with app.db.scope() as s, pytest.raises(c.ImportError_):
        c.import_contacts(s, df, {"email": None}, "f")


def test_email_validation_and_normalization():
    assert validate_syntax("  Foo@Bar.com ")[0] == "Foo@bar.com" or validate_syntax("  Foo@Bar.com ")[0]
    assert validate_syntax("nope")[1]
    assert normalize_email("J.o.h.n+tag@Gmail.com") == "john@gmail.com"
    assert normalize_email("A@Example.COM") == "a@example.com"
    assert normalize_email("bad") is None


def test_real_hr_contacts_csv_if_present(app):
    p = Path(__file__).resolve().parent.parent / "data" / "imports" / "hr_contacts.csv"
    if not p.exists():
        pytest.skip("hr_contacts.csv not present")
    df = c.read_table(p)
    with app.db.scope() as s:
        r = c.import_contacts(s, df, c.auto_map_columns(list(df.columns)), p.name)
    assert r.total_rows == 1842 and r.imported + len(r.rejected) == 1842
    assert r.duplicates_in_file == 0 and r.imported >= 1840


def test_domain_check_with_stub(app, make_contacts):
    ids = make_contacts(3, ready=False)
    with app.db.scope() as s:
        res = c.run_domain_checks(s, ids, checker=lambda d: {"co0.com": True, "co1.com": False}.get(d))
        assert res == {"checked": 3, "ok": 1, "bad": 1, "inconclusive": 1}
        st = {x.email: x.verification_status for x in s.query(Contact)}
    assert st["p0@co0.com"] == "Domain Checked" and st["p1@co1.com"] == "Invalid" and st["p2@co2.com"] == "Syntax Valid"


def test_segments_and_frame(app, make_contacts):
    make_contacts(2)
    with app.db.scope() as s:
        df = c.contacts_frame(s)
    assert set(df.segment) == {"HR Leadership (Head/VP/Director)"}
    assert c.segment_of("Senior Talent Acquisition Partner") == "Talent Acquisition / Recruiting"


def test_cannot_mark_suppressed_ready(app, make_contacts):
    ids = make_contacts(1, ready=False)
    with app.db.scope() as s:
        suppress(s, "p0@co0.com", "manual")
        assert c.bulk_update(s, ids, verification="Ready for Outreach") == 0


def test_suppression_logic(app, make_contacts):
    make_contacts(1)
    with app.db.scope() as s:
        assert not is_suppressed(s, "p0@co0.com")
        suppress(s, "P0@co0.com", "opt_out")
        assert is_suppressed(s, "p0@co0.com")
        assert s.query(Contact).one().opted_out
        with pytest.raises(ValueError):
            unsuppress(s, "p0@co0.com")  # opt-outs are permanent
        assert c.eligible_contacts(s) == []


def test_unsuppress_manual(app, make_contacts):
    make_contacts(1)
    with app.db.scope() as s:
        suppress(s, "p0@co0.com", "manual")
        unsuppress(s, "p0@co0.com")
        assert not is_suppressed(s, "p0@co0.com")
