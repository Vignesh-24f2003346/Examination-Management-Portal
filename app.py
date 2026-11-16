import os
import sqlite3
from datetime import date, datetime
from functools import wraps

from markupsafe import Markup, escape
from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE, "emp.db")

app = Flask(__name__)
app.secret_key = "emp-secret-key"

EXAM_TYPES = ["Viva", "Practical", "Project Demo", "Assessment"]

# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','examiner','student')),
    phone TEXT,
    department TEXT,
    status TEXT NOT NULL DEFAULT 'Active',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS courses(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'Active'
);
CREATE TABLE IF NOT EXISTS exams(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id),
    name TEXT NOT NULL,
    exam_type TEXT NOT NULL,
    duration INTEGER NOT NULL,
    max_marks REAL NOT NULL,
    description TEXT,
    slot_create_start TEXT NOT NULL,
    slot_create_end TEXT NOT NULL,
    book_start TEXT NOT NULL,
    book_end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Draft'
);
CREATE TABLE IF NOT EXISTS exam_examiners(
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    examiner_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY(exam_id, examiner_id)
);
CREATE TABLE IF NOT EXISTS rubrics(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    criterion TEXT NOT NULL,
    max_marks REAL NOT NULL,
    weightage REAL NOT NULL DEFAULT 0,
    description TEXT
);
CREATE TABLE IF NOT EXISTS slots(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    examiner_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    available_seats INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'Available'
);
CREATE TABLE IF NOT EXISTS bookings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES users(id),
    slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    exam_id INTEGER NOT NULL REFERENCES exams(id),
    booked_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Booked'
);
CREATE TABLE IF NOT EXISTS evaluations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER UNIQUE NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    remarks TEXT,
    total REAL NOT NULL,
    evaluated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scores(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    rubric_id INTEGER NOT NULL REFERENCES rubrics(id),
    marks REAL NOT NULL
);
"""


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    if not con.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        con.execute(
            "INSERT INTO users(name,email,password_hash,role,status) VALUES(?,?,?,?,?)",
            ("Administrator", "admin@emp.com", generate_password_hash("admin123"),
             "admin", "Active"))
    con.commit()
    con.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def q(sql, args=(), one=False):
    rows = get_db().execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def run(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur


EXAM_Q = ("SELECT e.*, c.code AS course_code, c.name AS course_name "
          "FROM exams e JOIN courses c ON c.id=e.course_id ")
SLOT_Q = ("SELECT s.*, e.name AS exam_name, e.status AS exam_status, e.book_start, "
          "e.book_end, c.code AS course_code, u.name AS examiner_name "
          "FROM slots s JOIN exams e ON e.id=s.exam_id "
          "JOIN courses c ON c.id=e.course_id JOIN users u ON u.id=s.examiner_id ")
BOOKING_Q = ("SELECT b.*, st.name AS student_name, st.email AS student_email, s.date, "
             "s.start_time, s.end_time, s.examiner_id, ex.name AS examiner_name, "
             "e.name AS exam_name, e.book_end, e.max_marks, c.code AS course_code "
             "FROM bookings b JOIN users st ON st.id=b.student_id "
             "JOIN slots s ON s.id=b.slot_id JOIN users ex ON ex.id=s.examiner_id "
             "JOIN exams e ON e.id=b.exam_id JOIN courses c ON c.id=e.course_id ")
RESULT_Q = ("SELECT ev.id, ev.remarks, ev.total, ev.evaluated_at, b.id AS booking_id, "
            "e.name AS exam_name, e.max_marks, c.code AS course_code, "
            "ex.name AS examiner_name, s.date "
            "FROM evaluations ev JOIN bookings b ON b.id=ev.booking_id "
            "JOIN slots s ON s.id=b.slot_id JOIN users ex ON ex.id=s.examiner_id "
            "JOIN exams e ON e.id=b.exam_id JOIN courses c ON c.id=e.course_id "
            "WHERE b.student_id=? ORDER BY ev.evaluated_at DESC")


def sync_slot(slot_id):
    """Recalculate available seats; auto-close (Full) / reopen a slot."""
    s = q("SELECT * FROM slots WHERE id=?", (slot_id,), one=True)
    taken = q("SELECT COUNT(*) n FROM bookings WHERE slot_id=? AND status!='Cancelled'",
              (slot_id,), one=True)["n"]
    avail = max(s["capacity"] - taken, 0)
    status = s["status"]
    if status in ("Available", "Full"):
        status = "Full" if avail <= 0 else "Available"
    get_db().execute("UPDATE slots SET available_seats=?, status=? WHERE id=?",
                     (avail, status, slot_id))


# ---------------------------------------------------------------- helpers
def today():
    return date.today().isoformat()


def booking_started(exam_status, book_start):
    return exam_status in ("Booking Open", "Closed", "Completed") or today() >= book_start


def current_user():
    uid = session.get("uid")
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True) if uid else None


@app.context_processor
def inject():
    return dict(me=current_user(), today=today())


def role_required(role):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **k):
            u = current_user()
            if not u:
                flash("Please log in first.", "warning")
                return redirect(url_for("login"))
            if u["role"] != role:
                abort(403)
            if role == "examiner" and u["status"] != "Approved":
                return render_template("pending.html")
            return f(*a, **k)
        return wrapper
    return deco


def F(name, label, type="text", value="", options=None, req=True):
    return dict(name=name, label=label, type=type, options=options, req=req,
                value="" if value is None else value)


def val(obj, key, default=""):
    if request.method == "POST":
        return request.form.get(key, default)
    return obj[key] if obj is not None else default


def form_page(title, fields, back, back_label="Cancel", submit="Save", hint=""):
    return render_template("form.html", title=title, fields=fields, back=back,
                           back_label=back_label, submit=submit, hint=hint)


BADGE = {"Active": "success", "Approved": "success", "Available": "success",
         "Booked": "primary", "Booking Open": "success", "Completed": "dark",
         "Pending": "warning", "Draft": "warning", "Slot Creation": "info",
         "Full": "danger", "Closed": "secondary", "Deactivated": "danger",
         "Cancelled": "danger", "Inactive": "secondary"}


@app.template_global()
def badge(text):
    """Coloured label for a status value."""
    return Markup('<span class="badge text-bg-%s">%s</span>'
                  % (BADGE.get(text, "secondary"), escape(text)))


def valid_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date().isoformat()


def valid_time(s):
    return datetime.strptime(s, "%H:%M").strftime("%H:%M")


# ---------------------------------------------------------------- auth
@app.route("/")
def index():
    return render_template("home.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM users WHERE email=?",
              (request.form.get("email", "").strip().lower(),), one=True)
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = u["id"]
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "danger")
    return form_page("Login", [F("email", "Email", "email", val(None, "email")),
                               F("password", "Password", "password")],
                     url_for("register"), "Register",
                     "Login")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        email = f.get("email", "").strip().lower()
        role = f.get("role")
        if role not in ("student", "examiner") or not f.get("name", "").strip():
            flash("Please fill in all fields correctly.", "danger")
        elif q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
            flash("An account with this email already exists.", "danger")
        elif len(f.get("password", "")) < 6:
            flash("Password must be at least 6 characters.", "danger")
        else:
            run("INSERT INTO users(name,email,password_hash,role,phone,department,status) "
                "VALUES(?,?,?,?,?,?,?)",
                (f["name"].strip(), email, generate_password_hash(f["password"]), role,
                 f.get("phone", "").strip(), f.get("department", "").strip(),
                 "Pending" if role == "examiner" else "Active"))
            flash("Registration successful. " + (
                "Please wait for the admin to approve your account."
                if role == "examiner" else "You can log in now."), "success")
            return redirect(url_for("login"))
    fields = [F("role", "Register as", "select", val(None, "role", "student"),
                [("student", "Student"), ("examiner", "Examiner")]),
              F("name", "Full Name", value=val(None, "name")),
              F("email", "Email", "email", val(None, "email")),
              F("password", "Password (min 6 characters)", "password"),
              F("phone", "Contact Number", value=val(None, "phone")),
              F("department", "Department", value=val(None, "department"))]
    return form_page("Register", fields, url_for("login"), "Back to login", "Register")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    u = current_user()
    if not u:
        return redirect(url_for("login"))
    return redirect(url_for({"admin": "admin_dashboard", "examiner": "examiner_dashboard",
                             "student": "student_dashboard"}[u["role"]]))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    u = current_user()
    if not u:
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        pw = f.get("password", "")
        if not f.get("name", "").strip():
            flash("Name is required.", "danger")
        elif pw and len(pw) < 6:
            flash("Password must be at least 6 characters.", "danger")
        else:
            run("UPDATE users SET name=?, phone=?, department=? WHERE id=?",
                (f["name"].strip(), f.get("phone", "").strip(),
                 f.get("department", "").strip(), u["id"]))
            if pw:
                run("UPDATE users SET password_hash=? WHERE id=?",
                    (generate_password_hash(pw), u["id"]))
            flash("Profile updated.", "success")
            return redirect(url_for("profile"))
    fields = [F("name", "Full Name", value=val(u, "name")),
              F("phone", "Contact Number", value=val(u, "phone"), req=False),
              F("department", "Department", value=val(u, "department"), req=False),
              F("password", "New Password (leave blank to keep current)", "password", req=False)]
    return form_page("Edit Profile - " + u["email"], fields, url_for("dashboard"))


# ================================================================= ADMIN
@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    items = [("Courses", "courses", "", "courses"),
             ("Examinations", "exams", "", "admin_exams"),
             ("Examiners", "users", " WHERE role='examiner'", "admin_examiners"),
             ("Students", "users", " WHERE role='student'", "admin_students"),
             ("Examination Slots", "slots", "", "admin_slots"),
             ("Bookings", "bookings", "", "admin_bookings")]
    stats = [(label, q(f"SELECT COUNT(*) n FROM {t}{w}", one=True)["n"], ep)
             for label, t, w, ep in items]
    pending = q("SELECT COUNT(*) n FROM users WHERE role='examiner' AND status='Pending'",
                one=True)["n"]
    return render_template("admin/dashboard.html", stats=stats, pending=pending)


# ---- courses
@app.route("/admin/courses")
@role_required("admin")
def courses():
    return render_template("admin/courses.html",
                           courses=q("SELECT * FROM courses ORDER BY id"))


@app.route("/admin/courses/new", methods=["GET", "POST"])
@app.route("/admin/courses/<int:cid>/edit", methods=["GET", "POST"])
@role_required("admin")
def course_form(cid=None):
    c = q("SELECT * FROM courses WHERE id=?", (cid,), one=True) if cid else None
    if cid and not c:
        abort(404)
    if request.method == "POST":
        f = request.form
        code, name = f.get("code", "").strip(), f.get("name", "").strip()
        dup = q("SELECT id FROM courses WHERE code=?", (code,), one=True)
        if not code or not name or f.get("status") not in ("Active", "Inactive"):
            flash("Course code, name and status are required.", "danger")
        elif dup and (not c or dup["id"] != c["id"]):
            flash("Course code already exists.", "danger")
        else:
            data = (code, name, f.get("description", "").strip(), f["status"])
            if c:
                run("UPDATE courses SET code=?,name=?,description=?,status=? WHERE id=?",
                    data + (cid,))
            else:
                run("INSERT INTO courses(code,name,description,status) VALUES(?,?,?,?)", data)
            flash("Course saved.", "success")
            return redirect(url_for("courses"))
    fields = [F("code", "Course Code", value=val(c, "code")),
              F("name", "Course Name", value=val(c, "name")),
              F("description", "Description", "textarea", val(c, "description"), req=False),
              F("status", "Status", "select", val(c, "status", "Active"),
                [("Active", "Active"), ("Inactive", "Inactive")])]
    return form_page("Edit Course" if c else "Add Course", fields, url_for("courses"))


@app.route("/admin/courses/<int:cid>/delete", methods=["POST"])
@role_required("admin")
def course_delete(cid):
    if q("SELECT 1 FROM exams WHERE course_id=?", (cid,), one=True):
        flash("This course has examinations and cannot be removed. Set it to Inactive instead.",
              "danger")
    else:
        run("DELETE FROM courses WHERE id=?", (cid,))
        flash("Course removed.", "success")
    return redirect(url_for("courses"))


# ---- examinations
@app.route("/admin/exams")
@role_required("admin")
def admin_exams():
    return render_template("admin/exams.html", exams=q(EXAM_Q + "ORDER BY e.id"))


@app.route("/admin/exams/new", methods=["GET", "POST"])
@app.route("/admin/exams/<int:eid>/edit", methods=["GET", "POST"])
@role_required("admin")
def exam_form(eid=None):
    e = q("SELECT * FROM exams WHERE id=?", (eid,), one=True) if eid else None
    if eid and not e:
        abort(404)
    course_rows = q("SELECT * FROM courses ORDER BY code")
    if not course_rows:
        flash("Create a course first.", "warning")
        return redirect(url_for("courses"))
    if request.method == "POST":
        f = request.form
        try:
            d = (int(f["course_id"]), f["name"].strip(), f["exam_type"], int(f["duration"]),
                 float(f["max_marks"]), f.get("description", "").strip(),
                 valid_date(f["slot_create_start"]), valid_date(f["slot_create_end"]),
                 valid_date(f["book_start"]), valid_date(f["book_end"]))
            ok = (d[1] and d[2] in EXAM_TYPES and d[3] > 0 and d[4] > 0
                  and d[6] <= d[7] and d[8] <= d[9]
                  and q("SELECT 1 FROM courses WHERE id=?", (d[0],), one=True))
        except (ValueError, KeyError):
            ok = False
        if not ok:
            flash("Invalid input. Check the values and make sure each end date is not "
                  "before its start date.", "danger")
        else:
            if e:
                run("UPDATE exams SET course_id=?,name=?,exam_type=?,duration=?,max_marks=?,"
                    "description=?,slot_create_start=?,slot_create_end=?,book_start=?,"
                    "book_end=? WHERE id=?", d + (eid,))
            else:
                eid = run("INSERT INTO exams(course_id,name,exam_type,duration,max_marks,"
                          "description,slot_create_start,slot_create_end,book_start,book_end)"
                          " VALUES(?,?,?,?,?,?,?,?,?,?)", d).lastrowid
            flash("Examination saved.", "success")
            return redirect(url_for("exam_detail", eid=eid))
    fields = [
        F("course_id", "Course", "select", val(e, "course_id"),
          [(c["id"], f"{c['code']} - {c['name']}") for c in course_rows]),
        F("name", "Examination Name", value=val(e, "name")),
        F("exam_type", "Examination Type", "select", val(e, "exam_type"),
          [(t, t) for t in EXAM_TYPES]),
        F("duration", "Duration (minutes)", "number", val(e, "duration")),
        F("max_marks", "Maximum Marks", "number", val(e, "max_marks")),
        F("slot_create_start", "Slot Creation Start Date", "date", val(e, "slot_create_start")),
        F("slot_create_end", "Slot Creation End Date", "date", val(e, "slot_create_end")),
        F("book_start", "Slot Booking Start Date", "date", val(e, "book_start")),
        F("book_end", "Slot Booking End Date", "date", val(e, "book_end")),
        F("description", "Description", "textarea", val(e, "description"), req=False)]
    return form_page("Edit Examination" if e else "Add Examination", fields,
                     url_for("admin_exams"))


@app.route("/admin/exams/<int:eid>")
@role_required("admin")
def exam_detail(eid):
    e = q(EXAM_Q + "WHERE e.id=?", (eid,), one=True)
    if not e:
        abort(404)
    rubrics = q("SELECT * FROM rubrics WHERE exam_id=? ORDER BY id", (eid,))
    examiners = q("SELECT u.* FROM users u JOIN exam_examiners x ON x.examiner_id=u.id "
                  "WHERE x.exam_id=?", (eid,))
    free = q("SELECT * FROM users WHERE role='examiner' AND status='Approved' AND id NOT IN "
             "(SELECT examiner_id FROM exam_examiners WHERE exam_id=?)", (eid,))
    slots = q(SLOT_Q + "WHERE s.exam_id=? ORDER BY s.date, s.start_time", (eid,))
    return render_template("admin/exam_detail.html", e=e, rubrics=rubrics, examiners=examiners,
                           free=free, slots=slots,
                           rub_total=sum(r["max_marks"] for r in rubrics))


@app.route("/admin/exams/<int:eid>/delete", methods=["POST"])
@role_required("admin")
def exam_delete(eid):
    if q("SELECT 1 FROM bookings WHERE exam_id=?", (eid,), one=True):
        flash("This examination has bookings and cannot be removed.", "danger")
        return redirect(url_for("exam_detail", eid=eid))
    run("DELETE FROM exams WHERE id=?", (eid,))
    flash("Examination removed.", "success")
    return redirect(url_for("admin_exams"))


@app.route("/admin/exams/<int:eid>/status/<action>", methods=["POST"])
@role_required("admin")
def exam_status(eid, action):
    states = {"open_slots": "Slot Creation", "close_slots": "Draft",
              "open_booking": "Booking Open", "close_booking": "Closed",
              "complete": "Completed"}
    if action not in states or not q("SELECT 1 FROM exams WHERE id=?", (eid,), one=True):
        abort(404)
    run("UPDATE exams SET status=? WHERE id=?", (states[action], eid))
    flash("Examination status changed to " + states[action] + ".", "success")
    return redirect(url_for("exam_detail", eid=eid))


@app.route("/admin/exams/<int:eid>/examiners", methods=["POST"])
@role_required("admin")
def exam_assign(eid):
    uid = request.form.get("examiner_id", type=int)
    if uid and q("SELECT 1 FROM users WHERE id=? AND role='examiner' AND status='Approved'",
                 (uid,), one=True):
        run("INSERT OR IGNORE INTO exam_examiners(exam_id,examiner_id) VALUES(?,?)", (eid, uid))
        flash("Examiner assigned.", "success")
    else:
        flash("Select an approved examiner.", "danger")
    return redirect(url_for("exam_detail", eid=eid))


@app.route("/admin/exams/<int:eid>/examiners/<int:uid>/remove", methods=["POST"])
@role_required("admin")
def exam_unassign(eid, uid):
    if q("SELECT 1 FROM slots WHERE exam_id=? AND examiner_id=?", (eid, uid), one=True):
        flash("This examiner has slots for the examination. Change the slot examiner first.",
              "danger")
    else:
        run("DELETE FROM exam_examiners WHERE exam_id=? AND examiner_id=?", (eid, uid))
        flash("Examiner removed from examination.", "success")
    return redirect(url_for("exam_detail", eid=eid))


# ---- rubrics
@app.route("/admin/exams/<int:eid>/rubrics/new", methods=["GET", "POST"])
@app.route("/admin/rubrics/<int:rid>/edit", methods=["GET", "POST"])
@role_required("admin")
def rubric_form(eid=None, rid=None):
    r = q("SELECT * FROM rubrics WHERE id=?", (rid,), one=True) if rid else None
    if rid and not r:
        abort(404)
    eid = r["exam_id"] if r else eid
    e = q("SELECT * FROM exams WHERE id=?", (eid,), one=True)
    if not e:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            crit, mm, w = f["criterion"].strip(), float(f["max_marks"]), float(f["weightage"])
            err = None
            if not crit or mm <= 0 or w < 0:
                err = "Enter a criterion name, positive marks and a valid weightage."
            else:
                others = q("SELECT COALESCE(SUM(max_marks),0) s FROM rubrics "
                           "WHERE exam_id=? AND id!=?", (eid, rid or 0), one=True)["s"]
                if others + mm > e["max_marks"]:
                    err = (f"Total rubric marks would exceed the examination's maximum "
                           f"marks ({e['max_marks']:g}).")
        except (ValueError, KeyError):
            err = "Invalid input."
        if err:
            flash(err, "danger")
        else:
            data = (crit, mm, w, f.get("description", "").strip())
            if r:
                run("UPDATE rubrics SET criterion=?,max_marks=?,weightage=?,description=? "
                    "WHERE id=?", data + (rid,))
            else:
                run("INSERT INTO rubrics(criterion,max_marks,weightage,description,exam_id) "
                    "VALUES(?,?,?,?,?)", data + (eid,))
            flash("Rubric saved.", "success")
            return redirect(url_for("exam_detail", eid=eid))
    fields = [F("criterion", "Criterion Name", value=val(r, "criterion")),
              F("max_marks", "Maximum Marks", "number", val(r, "max_marks")),
              F("weightage", "Weightage (%)", "number", val(r, "weightage")),
              F("description", "Description", "textarea", val(r, "description"), req=False)]
    return form_page(("Edit" if r else "Add") + " Rubric - " + e["name"], fields,
                     url_for("exam_detail", eid=eid))


@app.route("/admin/rubrics/<int:rid>/delete", methods=["POST"])
@role_required("admin")
def rubric_delete(rid):
    r = q("SELECT * FROM rubrics WHERE id=?", (rid,), one=True)
    if not r:
        abort(404)
    if q("SELECT 1 FROM scores WHERE rubric_id=?", (rid,), one=True):
        flash("This criterion has already been used for evaluation and cannot be removed.",
              "danger")
    else:
        run("DELETE FROM rubrics WHERE id=?", (rid,))
        flash("Rubric removed.", "success")
    return redirect(url_for("exam_detail", eid=r["exam_id"]))


# ---- examiners / students
@app.route("/admin/examiners")
@role_required("admin")
def admin_examiners():
    rows = q("SELECT u.*, (SELECT COUNT(*) FROM slots WHERE examiner_id=u.id) AS n_slots "
             "FROM users u WHERE role='examiner' ORDER BY u.id")
    return render_template("admin/examiners.html", examiners=rows)


@app.route("/admin/examiners/new", methods=["GET", "POST"])
@role_required("admin")
def examiner_add():
    if request.method == "POST":
        f = request.form
        email = f.get("email", "").strip().lower()
        if not f.get("name", "").strip() or not email:
            flash("Name and email are required.", "danger")
        elif q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
            flash("An account with this email already exists.", "danger")
        elif len(f.get("password", "")) < 6:
            flash("Password must be at least 6 characters.", "danger")
        else:
            run("INSERT INTO users(name,email,password_hash,role,phone,department,status) "
                "VALUES(?,?,?,'examiner',?,?,'Approved')",
                (f["name"].strip(), email, generate_password_hash(f["password"]),
                 f.get("phone", "").strip(), f.get("department", "").strip()))
            flash("Examiner added and approved.", "success")
            return redirect(url_for("admin_examiners"))
    fields = [F("name", "Full Name", value=val(None, "name")),
              F("email", "Email", "email", val(None, "email")),
              F("password", "Password (min 6 characters)", "password"),
              F("phone", "Contact Number", value=val(None, "phone")),
              F("department", "Department", value=val(None, "department"))]
    return form_page("Add Examiner", fields, url_for("admin_examiners"))


@app.route("/admin/examiners/<int:uid>/<action>", methods=["POST"])
@role_required("admin")
def examiner_action(uid, action):
    states = {"approve": "Approved", "activate": "Approved", "deactivate": "Deactivated"}
    if action not in states or not q("SELECT 1 FROM users WHERE id=? AND role='examiner'",
                                     (uid,), one=True):
        abort(404)
    run("UPDATE users SET status=? WHERE id=?", (states[action], uid))
    flash("Examiner status updated to " + states[action] + ".", "success")
    return redirect(url_for("admin_examiners"))


@app.route("/admin/students")
@role_required("admin")
def admin_students():
    rows = q("SELECT u.*, (SELECT COUNT(*) FROM bookings WHERE student_id=u.id) AS n_bookings "
             "FROM users u WHERE role='student' ORDER BY u.id")
    return render_template("admin/students.html", students=rows)


# ---- slots / bookings
@app.route("/admin/slots")
@role_required("admin")
def admin_slots():
    slots = []
    for s in q(SLOT_Q + "ORDER BY s.date, s.start_time"):
        opts = q("SELECT u.* FROM users u JOIN exam_examiners x ON x.examiner_id=u.id "
                 "WHERE x.exam_id=? AND u.status='Approved'", (s["exam_id"],))
        slots.append(dict(s, options=opts))
    return render_template("admin/slots.html", slots=slots)


@app.route("/admin/slots/<int:sid>/examiner", methods=["POST"])
@role_required("admin")
def slot_examiner(sid):
    s = q("SELECT * FROM slots WHERE id=?", (sid,), one=True)
    if not s:
        abort(404)
    uid = request.form.get("examiner_id", type=int)
    if uid and q("SELECT 1 FROM exam_examiners x JOIN users u ON u.id=x.examiner_id "
                 "WHERE x.exam_id=? AND x.examiner_id=? AND u.status='Approved'",
                 (s["exam_id"], uid), one=True):
        run("UPDATE slots SET examiner_id=? WHERE id=?", (uid, sid))
        flash("Slot examiner changed.", "success")
    else:
        flash("Choose an approved examiner assigned to this examination.", "danger")
    return redirect(url_for("admin_slots"))


@app.route("/admin/bookings")
@role_required("admin")
def admin_bookings():
    return render_template("admin/bookings.html", bookings=q(BOOKING_Q + "ORDER BY b.id DESC"))


@app.route("/admin/bookings/<int:bid>/reschedule", methods=["GET", "POST"])
@role_required("admin")
def reschedule(bid):
    b = q(BOOKING_Q + "WHERE b.id=?", (bid,), one=True)
    if not b:
        abort(404)
    if b["status"] != "Booked":
        flash("Only active bookings can be rescheduled.", "danger")
        return redirect(url_for("admin_bookings"))
    options = q(SLOT_Q + "WHERE s.exam_id=? AND s.id!=? AND s.status='Available' "
                "AND s.available_seats>0 ORDER BY s.date, s.start_time",
                (b["exam_id"], b["slot_id"]))
    if request.method == "POST":
        new_id = request.form.get("slot_id", type=int)
        if new_id not in [o["id"] for o in options]:
            flash("Choose an available slot.", "danger")
        else:
            db = get_db()
            db.execute("UPDATE bookings SET slot_id=? WHERE id=?", (new_id, bid))
            sync_slot(b["slot_id"])
            sync_slot(new_id)
            db.commit()
            flash("Booking rescheduled.", "success")
            return redirect(url_for("admin_bookings"))
    return render_template("admin/reschedule.html", b=b, options=options)


@app.route("/admin/search")
@role_required("admin")
def admin_search():
    term = request.args.get("q", "").strip()
    res = dict(students=[], examiners=[], exams=[], bookings=[])
    if term:
        like, qid = f"%{term}%", int(term) if term.isdigit() else -1
        for role, key in (("student", "students"), ("examiner", "examiners")):
            res[key] = q("SELECT * FROM users WHERE role=? AND (name LIKE ? OR email LIKE ? "
                         "OR id=?)", (role, like, like, qid))
        res["exams"] = q(EXAM_Q + "WHERE e.name LIKE ? OR c.code LIKE ? OR c.name LIKE ? "
                         "OR e.id=?", (like, like, like, qid))
        res["bookings"] = q(BOOKING_Q + "WHERE b.id=? OR st.name LIKE ? OR e.name LIKE ? "
                            "ORDER BY b.id DESC", (qid, like, like))
    return render_template("admin/search.html", term=term, **res)


# ============================================================== EXAMINER
@app.route("/examiner")
@role_required("examiner")
def examiner_dashboard():
    u = current_user()
    exams = []
    for e in q(EXAM_Q + "JOIN exam_examiners x ON x.exam_id=e.id WHERE x.examiner_id=? "
               "ORDER BY e.id", (u["id"],)):
        can_create = (e["status"] == "Slot Creation"
                      and e["slot_create_start"] <= today() <= e["slot_create_end"])
        exams.append(dict(e, can_create=can_create))
    slots = [dict(s, editable=not booking_started(s["exam_status"], s["book_start"]))
             for s in q(SLOT_Q + "WHERE s.examiner_id=? ORDER BY s.date, s.start_time",
                        (u["id"],))]
    booked = sum(s["capacity"] - s["available_seats"] for s in slots)
    pending = q("SELECT COUNT(*) n FROM bookings b JOIN slots s ON s.id=b.slot_id "
                "WHERE s.examiner_id=? AND b.status='Booked'", (u["id"],), one=True)["n"]
    return render_template("examiner/dashboard.html", exams=exams, slots=slots,
                           booked=booked, pending=pending)


@app.route("/examiner/slots/new", methods=["GET", "POST"])
@app.route("/examiner/slots/<int:sid>/edit", methods=["GET", "POST"])
@role_required("examiner")
def slot_form(sid=None):
    u = current_user()
    s = q(SLOT_Q + "WHERE s.id=?", (sid,), one=True) if sid else None
    if sid and not s:
        abort(404)
    if s:
        if s["examiner_id"] != u["id"]:
            abort(403)
        if booking_started(s["exam_status"], s["book_start"]):
            flash("Slots cannot be changed once booking has started.", "danger")
            return redirect(url_for("examiner_dashboard"))
        choices = [(s["exam_id"], f"{s['course_code']} - {s['exam_name']}")]
    else:
        rows = q(EXAM_Q + "JOIN exam_examiners x ON x.exam_id=e.id WHERE x.examiner_id=? "
                 "AND e.status='Slot Creation' AND e.slot_create_start<=? "
                 "AND e.slot_create_end>=?", (u["id"], today(), today()))
        if not rows:
            flash("Slot creation is not open for any of your examinations.", "warning")
            return redirect(url_for("examiner_dashboard"))
        choices = [(e["id"], f"{e['course_code']} - {e['name']}") for e in rows]
    if request.method == "POST":
        f = request.form
        err = None
        try:
            exam_id = s["exam_id"] if s else int(f["exam_id"])
            if exam_id not in [c[0] for c in choices]:
                raise ValueError
            d, st, en = valid_date(f["date"]), valid_time(f["start_time"]), valid_time(f["end_time"])
            cap = int(f["capacity"])
            if en <= st:
                err = "End time must be after start time."
            elif cap < 1:
                err = "Capacity must be at least 1."
            elif s and cap < s["capacity"] - s["available_seats"]:
                err = "Capacity cannot be lower than the number of booked students."
        except (ValueError, KeyError):
            err = "Invalid input, or slot creation is not open for that examination."
        if err:
            flash(err, "danger")
        else:
            db = get_db()
            if s:
                db.execute("UPDATE slots SET date=?,start_time=?,end_time=?,capacity=? "
                           "WHERE id=?", (d, st, en, cap, sid))
                slot_id = sid
            else:
                slot_id = db.execute(
                    "INSERT INTO slots(exam_id,examiner_id,date,start_time,end_time,capacity,"
                    "available_seats,status) VALUES(?,?,?,?,?,?,?,'Available')",
                    (exam_id, u["id"], d, st, en, cap, cap)).lastrowid
            sync_slot(slot_id)
            db.commit()
            flash("Slot saved.", "success")
            return redirect(url_for("examiner_dashboard"))
    fields = [F("exam_id", "Examination", "select", val(s, "exam_id"), choices),
              F("date", "Date", "date", val(s, "date")),
              F("start_time", "Start Time", "time", val(s, "start_time")),
              F("end_time", "End Time", "time", val(s, "end_time")),
              F("capacity", "Maximum Student Capacity", "number", val(s, "capacity"))]
    return form_page("Edit Slot" if s else "Create Slot", fields, url_for("examiner_dashboard"))


@app.route("/examiner/slots/<int:sid>/delete", methods=["POST"])
@role_required("examiner")
def slot_delete(sid):
    s = q(SLOT_Q + "WHERE s.id=?", (sid,), one=True)
    if not s:
        abort(404)
    if s["examiner_id"] != current_user()["id"]:
        abort(403)
    if booking_started(s["exam_status"], s["book_start"]):
        flash("Slots cannot be removed once booking has started.", "danger")
    else:
        run("DELETE FROM slots WHERE id=?", (sid,))
        flash("Slot removed.", "success")
    return redirect(url_for("examiner_dashboard"))


@app.route("/examiner/slots/<int:sid>")
@role_required("examiner")
def slot_detail(sid):
    s = q(SLOT_Q + "WHERE s.id=?", (sid,), one=True)
    if not s:
        abort(404)
    if s["examiner_id"] != current_user()["id"]:
        abort(403)
    bookings = q(BOOKING_Q + "WHERE b.slot_id=? ORDER BY b.id", (sid,))
    evals = {r["booking_id"]: r for r in q(
        "SELECT * FROM evaluations WHERE booking_id IN (SELECT id FROM bookings WHERE slot_id=?)",
        (sid,))}
    return render_template("examiner/slot_detail.html", s=s, bookings=bookings, evals=evals)


@app.route("/examiner/bookings/<int:bid>/evaluate", methods=["GET", "POST"])
@role_required("examiner")
def evaluate(bid):
    u = current_user()
    b = q(BOOKING_Q + "WHERE b.id=?", (bid,), one=True)
    if not b or b["status"] == "Cancelled":
        abort(404)
    if b["examiner_id"] != u["id"]:  # only students booked in own slots
        abort(403)
    rubrics = q("SELECT * FROM rubrics WHERE exam_id=? ORDER BY id", (b["exam_id"],))
    if not rubrics:
        flash("The admin has not defined rubrics for this examination yet.", "warning")
        return redirect(url_for("slot_detail", sid=b["slot_id"]))
    ev = q("SELECT * FROM evaluations WHERE booking_id=?", (bid,), one=True)
    existing = ({r["rubric_id"]: r["marks"] for r in
                 q("SELECT * FROM scores WHERE evaluation_id=?", (ev["id"],))} if ev else {})
    if request.method == "POST":
        try:
            marks = {}
            for r in rubrics:
                m = float(request.form[f"m_{r['id']}"])
                if m < 0 or m > r["max_marks"]:
                    raise ValueError
                marks[r["id"]] = m
        except (ValueError, KeyError):
            flash("Enter valid marks, not above each criterion's maximum.", "danger")
        else:
            db = get_db()
            remarks, now = request.form.get("remarks", "").strip(), datetime.now().isoformat(" ", "seconds")
            if ev:
                db.execute("UPDATE evaluations SET remarks=?,total=?,evaluated_at=? WHERE id=?",
                           (remarks, sum(marks.values()), now, ev["id"]))
                db.execute("DELETE FROM scores WHERE evaluation_id=?", (ev["id"],))
                evid = ev["id"]
            else:
                evid = db.execute("INSERT INTO evaluations(booking_id,remarks,total,evaluated_at)"
                                  " VALUES(?,?,?,?)", (bid, remarks, sum(marks.values()), now)).lastrowid
            for rid, m in marks.items():
                db.execute("INSERT INTO scores(evaluation_id,rubric_id,marks) VALUES(?,?,?)",
                           (evid, rid, m))
            db.execute("UPDATE bookings SET status='Completed' WHERE id=?", (bid,))
            if not q("SELECT 1 FROM bookings WHERE slot_id=? AND status='Booked'",
                     (b["slot_id"],), one=True):
                db.execute("UPDATE slots SET status='Completed' WHERE id=?", (b["slot_id"],))
            db.commit()
            flash("Marks and remarks submitted.", "success")
            return redirect(url_for("slot_detail", sid=b["slot_id"]))
    return render_template("examiner/evaluate.html", b=b, rubrics=rubrics,
                           existing=existing, ev=ev)


# =============================================================== STUDENT
def student_results(uid):
    out = []
    for r in q(RESULT_Q, (uid,)):
        scores = q("SELECT sc.marks, r.criterion, r.max_marks FROM scores sc "
                   "JOIN rubrics r ON r.id=sc.rubric_id WHERE sc.evaluation_id=?", (r["id"],))
        out.append(dict(r, scores=scores))
    return out


@app.route("/student")
@role_required("student")
def student_dashboard():
    uid, t = current_user()["id"], today()
    mine = q(BOOKING_Q + "WHERE b.student_id=? ORDER BY s.date, s.start_time", (uid,))
    taken = {b["exam_id"] for b in mine if b["status"] != "Cancelled"}
    open_exams = [e for e in q(EXAM_Q + "WHERE e.status='Booking Open' AND c.status='Active' "
                               "AND e.book_start<=? AND e.book_end>=?", (t, t))
                  if e["id"] not in taken]
    avail_slots = [s for s in q(SLOT_Q + "WHERE s.status='Available' AND s.available_seats>0 "
                                "AND e.status='Booking Open' AND e.book_start<=? AND e.book_end>=? "
                                "ORDER BY s.date, s.start_time", (t, t))
                   if s["exam_id"] not in taken]
    booked = [b for b in mine if b["status"] == "Booked"]
    return render_template("student/dashboard.html", open_exams=open_exams,
                           avail_slots=avail_slots, booked=booked,
                           upcoming=[b for b in booked if b["date"] >= t],
                           results=student_results(uid))


@app.route("/student/exams")
@role_required("student")
def student_exams():
    term, typ = request.args.get("q", "").strip(), request.args.get("type", "")
    status, course = request.args.get("status", ""), request.args.get("course", "")
    sql, args = EXAM_Q + "WHERE c.status='Active'", []
    if term:
        sql += " AND (e.name LIKE ? OR c.code LIKE ? OR c.name LIKE ?)"
        args += [f"%{term}%"] * 3
    if typ:
        sql += " AND e.exam_type=?"
        args.append(typ)
    if status:
        sql += " AND e.status=?"
        args.append(status)
    if course:
        sql += " AND e.course_id=?"
        args.append(course)
    return render_template("student/exams.html", exams=q(sql + " ORDER BY e.id DESC", args),
                           courses=q("SELECT * FROM courses WHERE status='Active' ORDER BY code"),
                           types=EXAM_TYPES, f=dict(q=term, type=typ, status=status, course=course),
                           statuses=["Draft", "Slot Creation", "Booking Open", "Closed", "Completed"])


@app.route("/student/exams/<int:eid>")
@role_required("student")
def student_exam(eid):
    e = q(EXAM_Q + "WHERE e.id=?", (eid,), one=True)
    if not e:
        abort(404)
    uid = current_user()["id"]
    mine = q("SELECT * FROM bookings WHERE student_id=? AND exam_id=? AND status!='Cancelled'",
             (uid, eid), one=True)
    can_book = (e["status"] == "Booking Open" and e["book_start"] <= today() <= e["book_end"]
                and not mine)
    rubrics = q("SELECT * FROM rubrics WHERE exam_id=?", (eid,))
    slots = q(SLOT_Q + "WHERE s.exam_id=? ORDER BY s.date, s.start_time", (eid,))
    return render_template("student/exam.html", e=e, slots=slots, mine=mine,
                           can_book=can_book, rubrics=rubrics)


@app.route("/student/book/<int:sid>", methods=["POST"])
@role_required("student")
def book(sid):
    uid, db = current_user()["id"], get_db()
    if not q("SELECT 1 FROM slots WHERE id=?", (sid,), one=True):
        abort(404)
    db.execute("BEGIN IMMEDIATE")  # lock so two students cannot take the last seat
    s = q(SLOT_Q + "WHERE s.id=?", (sid,), one=True)
    if s["exam_status"] != "Booking Open" or not (s["book_start"] <= today() <= s["book_end"]):
        err = "Slot booking is not open for this examination."
    elif s["status"] != "Available" or s["available_seats"] <= 0:
        err = "This slot is full or unavailable."
    elif q("SELECT 1 FROM bookings WHERE student_id=? AND exam_id=? AND status!='Cancelled'",
           (uid, s["exam_id"]), one=True):
        err = "You already have a booking for this examination."
    else:
        err = None
        db.execute("INSERT INTO bookings(student_id,slot_id,exam_id,booked_at,status) "
                   "VALUES(?,?,?,?,'Booked')",
                   (uid, sid, s["exam_id"], datetime.now().isoformat(" ", "seconds")))
        sync_slot(sid)
    if err:
        db.rollback()
        flash(err, "danger")
        return redirect(url_for("student_exam", eid=s["exam_id"]))
    db.commit()
    flash("Slot booked successfully.", "success")
    return redirect(url_for("student_bookings"))


@app.route("/student/bookings")
@role_required("student")
def student_bookings():
    rows = q(BOOKING_Q + "WHERE b.student_id=? ORDER BY b.id DESC", (current_user()["id"],))
    return render_template("student/bookings.html", bookings=rows)


@app.route("/student/bookings/<int:bid>/cancel", methods=["POST"])
@role_required("student")
def cancel(bid):
    b = q(BOOKING_Q + "WHERE b.id=?", (bid,), one=True)
    if not b or b["student_id"] != current_user()["id"]:
        abort(404)
    if b["status"] != "Booked":
        flash("Only active bookings can be cancelled.", "danger")
    elif today() > b["book_end"]:
        flash("The booking deadline has passed; this booking can no longer be cancelled.",
              "danger")
    else:
        db = get_db()
        db.execute("UPDATE bookings SET status='Cancelled' WHERE id=?", (bid,))
        sync_slot(b["slot_id"])
        db.commit()
        flash("Booking cancelled.", "success")
    return redirect(url_for("student_bookings"))


@app.route("/student/results")
@role_required("student")
def student_results_page():
    return render_template("student/results.html", results=student_results(current_user()["id"]))


init_db()

if __name__ == "__main__":
    app.run(debug=True)
