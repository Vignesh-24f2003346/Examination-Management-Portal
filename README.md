# Examination Management Portal (EMP)

Flask + Jinja2 + Bootstrap + SQLite. No JavaScript is used.

## Run
1. `pip install -r requirements.txt`
2. `python app.py`
3. Open http://127.0.0.1:5000

The database (`emp.db`) is created automatically on first run.

## Admin login (pre-seeded)
- Email: admin@emp.com
- Password: admin123

Examiners register themselves and need the admin's approval. Students register and log in directly.
Bootstrap CSS is loaded from a CDN, so the browser needs internet access.

## Who does what
- **Admin**: creates courses, examinations (with dates), rubrics; approves examiners and assigns them; opens slot creation and booking; can reschedule bookings and change a slot's examiner.
- **Examiner**: creates slots while slot creation is open; sees booked students; enters marks per rubric and remarks.
- **Student**: books a slot while booking is open; cancels before the deadline; sees results.

See VIVA_GUIDE.md for a 5-minute demo script and likely viva questions.
