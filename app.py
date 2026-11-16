
import csv
import hashlib
import io
import os

from datetime import datetime, date
from functools import wraps
from urllib.parse import quote_plus

from flask import (
    Flask, render_template, request,
    redirect, url_for, flash, session, g
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, UniqueConstraint, inspect, text
from werkzeug.security import generate_password_hash, check_password_hash


# --------------------------------------------------
# APP CONFIGURATION
# --------------------------------------------------

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "replace-with-your-own-random-secret-key"
)

app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

MYSQL_USER = "root"
MYSQL_PASSWORD = quote_plus(os.environ.get("MYSQL_PASSWORD", ""))
MYSQL_HOST = "localhost"
MYSQL_DATABASE = "aapnakharcha"

app.config["SQLALCHEMY_DATABASE_URI"] = (
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
    f"@{MYSQL_HOST}/{MYSQL_DATABASE}"
)

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# --------------------------------------------------
# DATABASE MODELS
# --------------------------------------------------

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)

    # Keep both fields for compatibility with the existing database.
    password = db.Column(db.String(255), nullable=True)
    password_hash = db.Column(db.String(255), nullable=True)


class Income(db.Model):
    __tablename__ = "income"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(100), nullable=False, default="Other")
    description = db.Column(db.String(255))
    date = db.Column(db.Date, nullable=False)


class Expense(db.Model):
    __tablename__ = "expenses"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(100), nullable=False, default="Other")
    description = db.Column(db.String(255))
    date = db.Column(db.Date, nullable=False)


class BankImport(db.Model):
    __tablename__ = "bank_imports"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    fingerprint = db.Column(db.String(64), nullable=False)
    transaction_type = db.Column(db.String(20), nullable=False)
    transaction_id = db.Column(db.Integer, nullable=False)
    imported_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "fingerprint",
            name="unique_user_transaction"
        ),
    )


class BankConnection(db.Model):
    __tablename__ = "bank_connections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    bank_name = db.Column(db.String(100), nullable=False)
    account_label = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default="Connected", nullable=False)
    connected_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_synced_at = db.Column(db.DateTime)


# --------------------------------------------------
# SAFE DATABASE COMPATIBILITY FIXES
# --------------------------------------------------

def update_existing_database():
    """
    Add only missing columns.
    Existing tables and transaction records are preserved.
    """

    inspector = inspect(db.engine)
    tables = inspector.get_table_names()

    if "users" in tables:
        columns = [
            column["name"]
            for column in inspector.get_columns("users")
        ]

        if "password_hash" not in columns:
            db.session.execute(text(
                "ALTER TABLE users ADD COLUMN "
                "password_hash VARCHAR(255) NULL"
            ))

        if "password" not in columns:
            db.session.execute(text(
                "ALTER TABLE users ADD COLUMN "
                "password VARCHAR(255) NULL"
            ))

    for table_name in ["income", "expenses"]:
        if table_name in tables:
            columns = [
                column["name"]
                for column in inspector.get_columns(table_name)
            ]

            if "category" not in columns:
                db.session.execute(text(
                    f"ALTER TABLE {table_name} "
                    "ADD COLUMN category VARCHAR(100) "
                    "NOT NULL DEFAULT 'Other'"
                ))

    db.session.commit()


# --------------------------------------------------
# BANK SETTINGS
# --------------------------------------------------

MAX_BANK_CONNECTIONS = 2

PREFERRED_BANKS = [
    "State Bank of India",
    "ICICI Bank"
]

DEMO_BANKS = [
    "State Bank of India",
    "HDFC Bank",
    "ICICI Bank",
    "Axis Bank",
    "Kotak Mahindra Bank",
    "Punjab National Bank"
]


# --------------------------------------------------
# KEEP ONLY TWO CONNECTED BANKS
# --------------------------------------------------

def enforce_bank_limit(user_id):

    connections = (
        BankConnection.query
        .filter_by(user_id=user_id, status="Connected")
        .order_by(
            BankConnection.connected_at.desc(),
            BankConnection.id.desc()
        )
        .all()
    )

    if len(connections) <= MAX_BANK_CONNECTIONS:
        return

    keep_ids = []

    # Prioritise SBI and ICICI.
    for preferred_bank in PREFERRED_BANKS:
        for connection in connections:
            if (
                connection.bank_name == preferred_bank
                and connection.id not in keep_ids
            ):
                keep_ids.append(connection.id)
                break

    # Fill remaining slots with newest connections.
    for connection in connections:
        if len(keep_ids) >= MAX_BANK_CONNECTIONS:
            break

        if connection.id not in keep_ids:
            keep_ids.append(connection.id)

    # Remove extra connection records only.
    for connection in connections:
        if connection.id not in keep_ids:
            db.session.delete(connection)

    db.session.commit()


# --------------------------------------------------
# LOGIN SESSION
# --------------------------------------------------

@app.before_request
def load_logged_in_user():

    user_id = session.get("user_id")

    g.user = (
        db.session.get(User, user_id)
        if user_id else None
    )


def login_required(view):

    @wraps(view)
    def wrapped_view(**kwargs):

        if g.user is None:
            return redirect(url_for("login"))

        return view(**kwargs)

    return wrapped_view


# --------------------------------------------------
# AUTOMATIC CATEGORY SUGGESTIONS
# --------------------------------------------------

def suggest_category(description, transaction_type):

    description = (description or "").lower()

    if transaction_type == "Income":

        if any(word in description for word in [
            "salary", "payroll", "wage"
        ]):
            return "Salary"

        if any(word in description for word in [
            "interest", "dividend", "investment"
        ]):
            return "Investment"

        if any(word in description for word in [
            "refund", "cashback"
        ]):
            return "Refund"

        return "Other"

    if any(word in description for word in [
        "restaurant", "food", "swiggy", "zomato"
    ]):
        return "Food"

    if any(word in description for word in [
        "uber", "ola", "petrol", "fuel", "metro"
    ]):
        return "Travel"

    if any(word in description for word in [
        "amazon", "flipkart", "shopping"
    ]):
        return "Shopping"

    if any(word in description for word in [
        "electricity", "water", "recharge", "bill"
    ]):
        return "Bills"

    if any(word in description for word in [
        "school", "college", "education", "course"
    ]):
        return "Education"

    return "Other"


# --------------------------------------------------
# DATE FORMAT HANDLING
# --------------------------------------------------

def parse_transaction_date(value):

    formats = [
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d/%m/%y",
        "%d %b %Y"
    ]

    for date_format in formats:
        try:
            return datetime.strptime(
                value.strip(), date_format
            ).date()
        except (ValueError, AttributeError):
            continue

    raise ValueError("Invalid date")


# --------------------------------------------------
# HOME
# --------------------------------------------------

@app.route("/")
def home():

    if g.user:
        return redirect(url_for("dashboard"))

    return redirect(url_for("login"))


# --------------------------------------------------
# REGISTER
# --------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not email or not password:
            flash("Please fill in all fields.", "error")

        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "error")

        elif User.query.filter_by(email=email).first():
            flash("Email already registered.", "error")

        else:

            hashed_password = generate_password_hash(password)

            new_user = User(
                username=username,
                email=email,
                password=hashed_password,
                password_hash=hashed_password
            )

            db.session.add(new_user)
            db.session.commit()

            flash(
                "Registration successful! Please log in.",
                "success"
            )

            return redirect(url_for("login"))

    return render_template("register.html")


# --------------------------------------------------
# LOGIN
# --------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()

        if user:

            stored_password = user.password_hash or user.password

            if (
                stored_password
                and check_password_hash(stored_password, password)
            ):

                session.clear()
                session["user_id"] = user.id

                flash("Welcome back!", "success")

                return redirect(url_for("dashboard"))

        flash("Incorrect email or password.", "error")

    return render_template("login.html")


# --------------------------------------------------
# LOGOUT
# --------------------------------------------------

@app.route("/logout", methods=["POST"])
@login_required
def logout():

    session.clear()

    flash("You have logged out successfully.", "success")

    return redirect(url_for("login"))


# --------------------------------------------------
# DASHBOARD
# --------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():

    total_income = db.session.query(
        func.coalesce(func.sum(Income.amount), 0)
    ).filter_by(user_id=g.user.id).scalar()

    total_expenses = db.session.query(
        func.coalesce(func.sum(Expense.amount), 0)
    ).filter_by(user_id=g.user.id).scalar()

    balance = total_income - total_expenses

    recent_income = (
        Income.query
        .filter_by(user_id=g.user.id)
        .order_by(Income.date.desc(), Income.id.desc())
        .limit(5)
        .all()
    )

    recent_expenses = (
        Expense.query
        .filter_by(user_id=g.user.id)
        .order_by(Expense.date.desc(), Expense.id.desc())
        .limit(5)
        .all()
    )

    return render_template(
        "dashboard.html",
        total_income=total_income,
        total_expenses=total_expenses,
        balance=balance,
        recent_income=recent_income,
        recent_expenses=recent_expenses
    )


# --------------------------------------------------
# ADD INCOME
# --------------------------------------------------

@app.route("/income", methods=["GET", "POST"])
@login_required
def income():

    if request.method == "POST":

        try:

            amount = float(request.form.get("amount", "").strip())

            category = request.form.get("category", "").strip()

            description = request.form.get(
                "description", ""
            ).strip()

            income_date = datetime.strptime(
                request.form.get("date", "").strip(),
                "%Y-%m-%d"
            ).date()

            if amount <= 0 or not category:
                raise ValueError

            new_income = Income(
                user_id=g.user.id,
                amount=amount,
                category=category,
                description=description or None,
                date=income_date
            )

            db.session.add(new_income)
            db.session.commit()

            flash("Income added successfully! 💗", "success")

            return redirect(url_for("dashboard"))

        except ValueError:

            flash(
                "Please enter a valid amount, category and date.",
                "error"
            )

    return render_template(
        "add_income.html",
        today=date.today().isoformat()
    )


# --------------------------------------------------
# ADD EXPENSE
# --------------------------------------------------

@app.route("/expenses", methods=["GET", "POST"])
@login_required
def expenses():

    if request.method == "POST":

        try:

            amount = float(request.form.get("amount", "").strip())

            category = request.form.get("category", "").strip()

            description = request.form.get(
                "description", ""
            ).strip()

            expense_date = datetime.strptime(
                request.form.get("date", "").strip(),
                "%Y-%m-%d"
            ).date()

            if amount <= 0 or not category:
                raise ValueError

            new_expense = Expense(
                user_id=g.user.id,
                amount=amount,
                category=category,
                description=description or None,
                date=expense_date
            )

            db.session.add(new_expense)
            db.session.commit()

            flash("Expense added successfully! 🧡", "success")

            return redirect(url_for("dashboard"))

        except ValueError:

            flash(
                "Please enter a valid amount, category and date.",
                "error"
            )

    return render_template(
        "add_expense.html",
        today=date.today().isoformat()
    )


# --------------------------------------------------
# IMPORT BANK STATEMENT (CSV)
# --------------------------------------------------

@app.route("/bank-import", methods=["GET", "POST"])
@login_required
def bank_import():

    if request.method == "POST":

        file = request.files.get("file")

        if not file or not file.filename:
            flash("Please select a CSV file.", "error")
            return redirect(url_for("bank_import"))

        if not file.filename.lower().endswith(".csv"):
            flash("Please upload a CSV file only.", "error")
            return redirect(url_for("bank_import"))

        try:

            content = file.read().decode("utf-8-sig")

            reader = csv.DictReader(io.StringIO(content))

            if not reader.fieldnames:
                flash("The CSV file is empty.", "error")
                return redirect(url_for("bank_import"))

            reader.fieldnames = [
                name.strip().lower() if name else ""
                for name in reader.fieldnames
            ]

            required_columns = {
                "date", "description", "amount", "type"
            }

            if not required_columns.issubset(set(reader.fieldnames)):
                flash(
                    "CSV must contain date, description, amount and type columns.",
                    "error"
                )
                return redirect(url_for("bank_import"))

            imported_count = 0
            duplicate_count = 0
            skipped_count = 0

            for row in reader:

                try:

                    transaction_date = parse_transaction_date(
                        row.get("date", "")
                    )

                    description = row.get(
                        "description", ""
                    ).strip()

                    amount_text = (
                        row.get("amount", "")
                        .replace(",", "")
                        .replace("₹", "")
                        .replace("INR", "")
                        .strip()
                    )

                    amount = abs(float(amount_text))

                    if amount <= 0:
                        raise ValueError

                    raw_type = row.get(
                        "type", ""
                    ).strip().lower()

                    if raw_type in ["income", "credit", "cr"]:
                        transaction_type = "Income"

                    elif raw_type in ["expense", "debit", "dr"]:
                        transaction_type = "Expense"

                    else:
                        raise ValueError

                    category = row.get("category", "").strip()

                    if not category:
                        category = suggest_category(
                            description,
                            transaction_type
                        )

                    fingerprint_text = (
                        f"{transaction_date}|{description.lower()}|"
                        f"{amount:.2f}|{transaction_type}"
                    )

                    fingerprint = hashlib.sha256(
                        fingerprint_text.encode("utf-8")
                    ).hexdigest()

                    existing = BankImport.query.filter_by(
                        user_id=g.user.id,
                        fingerprint=fingerprint
                    ).first()

                    if existing:
                        duplicate_count += 1
                        continue

                    if transaction_type == "Income":

                        transaction = Income(
                            user_id=g.user.id,
                            amount=amount,
                            category=category,
                            description=description or None,
                            date=transaction_date
                        )

                    else:

                        transaction = Expense(
                            user_id=g.user.id,
                            amount=amount,
                            category=category,
                            description=description or None,
                            date=transaction_date
                        )

                    db.session.add(transaction)
                    db.session.flush()

                    db.session.add(
                        BankImport(
                            user_id=g.user.id,
                            fingerprint=fingerprint,
                            transaction_type=transaction_type,
                            transaction_id=transaction.id
                        )
                    )

                    imported_count += 1

                except (ValueError, TypeError, AttributeError):
                    skipped_count += 1

            db.session.commit()

            flash(
                f"Import complete! {imported_count} imported, "
                f"{duplicate_count} duplicates skipped, "
                f"{skipped_count} invalid rows skipped.",
                "success"
            )

            return redirect(url_for("history"))

        except UnicodeDecodeError:

            db.session.rollback()

            flash(
                "Could not read this file. Please use a UTF-8 CSV file.",
                "error"
            )

        except Exception:

            db.session.rollback()

            app.logger.exception("Bank statement import failed")

            flash(
                "Import failed. Please check your CSV file.",
                "error"
            )

    return render_template("bank_import.html")


# --------------------------------------------------
# CONNECT BANK - MAXIMUM TWO
# --------------------------------------------------

@app.route("/connect-bank", methods=["GET", "POST"])
@login_required
def connect_bank():

    enforce_bank_limit(g.user.id)

    if request.method == "POST":

        bank_name = request.form.get(
            "bank_name", ""
        ).strip()

        if bank_name not in DEMO_BANKS:

            flash("Please select a valid demo bank.", "error")

            return redirect(url_for("connect_bank"))

        existing_bank = BankConnection.query.filter_by(
            user_id=g.user.id,
            bank_name=bank_name,
            status="Connected"
        ).first()

        if existing_bank:

            flash("This bank is already connected.", "error")

            return redirect(url_for("connect_bank"))

        current_count = BankConnection.query.filter_by(
            user_id=g.user.id,
            status="Connected"
        ).count()

        if current_count >= MAX_BANK_CONNECTIONS:

            flash(
                "You can connect a maximum of 2 banks. "
                "Disconnect one bank before connecting another.",
                "error"
            )

            return redirect(url_for("connect_bank"))

        connection = BankConnection(
            user_id=g.user.id,
            bank_name=bank_name,
            account_label="Demo account",
            status="Connected"
        )

        db.session.add(connection)
        db.session.commit()

        flash(
            f"{bank_name} demo account connected successfully! 💗",
            "success"
        )

        return redirect(url_for("connect_bank"))

    connections = (
        BankConnection.query
        .filter_by(user_id=g.user.id, status="Connected")
        .order_by(BankConnection.connected_at.desc())
        .all()
    )

    return render_template(
        "connect_bank.html",
        banks=DEMO_BANKS,
        connections=connections
    )


# --------------------------------------------------
# SYNC DEMO BANK TRANSACTIONS
# --------------------------------------------------

@app.route("/bank/sync/<int:connection_id>", methods=["POST"])
@login_required
def sync_demo_bank(connection_id):

    connection = BankConnection.query.filter_by(
        id=connection_id,
        user_id=g.user.id,
        status="Connected"
    ).first_or_404()

    today = date.today()

    demo_transactions = [
        {
            "key": "salary",
            "type": "Income",
            "amount": 25000,
            "category": "Salary",
            "description": f"DEMO - {connection.bank_name} salary credit"
        },
        {
            "key": "shopping",
            "type": "Expense",
            "amount": 1200,
            "category": "Shopping",
            "description": f"DEMO - {connection.bank_name} shopping"
        },
        {
            "key": "food",
            "type": "Expense",
            "amount": 450,
            "category": "Food",
            "description": f"DEMO - {connection.bank_name} food payment"
        },
        {
            "key": "interest",
            "type": "Income",
            "amount": 300,
            "category": "Investment",
            "description": f"DEMO - {connection.bank_name} interest credit"
        }
    ]

    imported_count = 0
    duplicate_count = 0

    try:

        for item in demo_transactions:

            fingerprint_text = (
                f"demo-bank-{connection.id}-{item['key']}"
            )

            fingerprint = hashlib.sha256(
                fingerprint_text.encode("utf-8")
            ).hexdigest()

            existing = BankImport.query.filter_by(
                user_id=g.user.id,
                fingerprint=fingerprint
            ).first()

            if existing:
                duplicate_count += 1
                continue

            if item["type"] == "Income":

                transaction = Income(
                    user_id=g.user.id,
                    amount=item["amount"],
                    category=item["category"],
                    description=item["description"],
                    date=today
                )

            else:

                transaction = Expense(
                    user_id=g.user.id,
                    amount=item["amount"],
                    category=item["category"],
                    description=item["description"],
                    date=today
                )

            db.session.add(transaction)
            db.session.flush()

            db.session.add(
                BankImport(
                    user_id=g.user.id,
                    fingerprint=fingerprint,
                    transaction_type=item["type"],
                    transaction_id=transaction.id
                )
            )

            imported_count += 1

        connection.last_synced_at = datetime.utcnow()

        db.session.commit()

        flash(
            f"Demo sync complete! {imported_count} transactions added, "
            f"{duplicate_count} duplicates skipped. 💗",
            "success"
        )

    except Exception:

        db.session.rollback()

        app.logger.exception("Demo bank sync failed")

        flash("Demo sync failed. Please try again.", "error")

    return redirect(url_for("connect_bank"))


# --------------------------------------------------
# DISCONNECT BANK
# --------------------------------------------------

@app.route("/bank/disconnect/<int:connection_id>", methods=["POST"])
@login_required
def disconnect_bank(connection_id):

    connection = BankConnection.query.filter_by(
        id=connection_id,
        user_id=g.user.id
    ).first_or_404()

    db.session.delete(connection)
    db.session.commit()

    flash("Demo bank disconnected successfully.", "success")

    return redirect(url_for("connect_bank"))


# --------------------------------------------------
# EDIT INCOME
# --------------------------------------------------

@app.route("/income/edit/<int:income_id>", methods=["GET", "POST"])
@login_required
def edit_income(income_id):

    item = Income.query.filter_by(
        id=income_id,
        user_id=g.user.id
    ).first_or_404()

    if request.method == "POST":

        try:

            amount = float(request.form.get("amount", "").strip())

            category = request.form.get(
                "category", ""
            ).strip()

            description = request.form.get(
                "description", ""
            ).strip()

            transaction_date = datetime.strptime(
                request.form.get("date", "").strip(),
                "%Y-%m-%d"
            ).date()

            if amount <= 0 or not category:
                raise ValueError

            item.amount = amount
            item.category = category
            item.description = description or None
            item.date = transaction_date

            db.session.commit()

            flash("Income updated successfully! 💗", "success")

            return redirect(url_for("history"))

        except ValueError:

            flash(
                "Please enter a valid amount, category and date.",
                "error"
            )

    return render_template("edit_income.html", item=item)


# --------------------------------------------------
# EDIT EXPENSE
# --------------------------------------------------

@app.route("/expense/edit/<int:expense_id>", methods=["GET", "POST"])
@login_required
def edit_expense(expense_id):

    item = Expense.query.filter_by(
        id=expense_id,
        user_id=g.user.id
    ).first_or_404()

    if request.method == "POST":

        try:

            amount = float(request.form.get("amount", "").strip())

            category = request.form.get(
                "category", ""
            ).strip()

            description = request.form.get(
                "description", ""
            ).strip()

            transaction_date = datetime.strptime(
                request.form.get("date", "").strip(),
                "%Y-%m-%d"
            ).date()

            if amount <= 0 or not category:
                raise ValueError

            item.amount = amount
            item.category = category
            item.description = description or None
            item.date = transaction_date

            db.session.commit()

            flash("Expense updated successfully! 🧡", "success")

            return redirect(url_for("history"))

        except ValueError:

            flash(
                "Please enter a valid amount, category and date.",
                "error"
            )

    return render_template("edit_expense.html", item=item)


# --------------------------------------------------
# DELETE INCOME
# --------------------------------------------------

@app.route("/income/delete/<int:income_id>", methods=["POST"])
@login_required
def delete_income(income_id):

    item = Income.query.filter_by(
        id=income_id,
        user_id=g.user.id
    ).first_or_404()

    db.session.delete(item)
    db.session.commit()

    flash("Income deleted successfully.", "success")

    return redirect(url_for("history"))


# --------------------------------------------------
# DELETE EXPENSE
# --------------------------------------------------

@app.route("/expense/delete/<int:expense_id>", methods=["POST"])
@login_required
def delete_expense(expense_id):

    item = Expense.query.filter_by(
        id=expense_id,
        user_id=g.user.id
    ).first_or_404()

    db.session.delete(item)
    db.session.commit()

    flash("Expense deleted successfully.", "success")

    return redirect(url_for("history"))


# --------------------------------------------------
# REPORTS
# --------------------------------------------------

@app.route("/reports")
@login_required
def reports():

    total_income = db.session.query(
        func.coalesce(func.sum(Income.amount), 0)
    ).filter_by(user_id=g.user.id).scalar()

    total_expenses = db.session.query(
        func.coalesce(func.sum(Expense.amount), 0)
    ).filter_by(user_id=g.user.id).scalar()

    balance = total_income - total_expenses

    expense_categories = (
        db.session.query(
            Expense.category,
            func.sum(Expense.amount).label("total")
        )
        .filter_by(user_id=g.user.id)
        .group_by(Expense.category)
        .all()
    )

    monthly_income = (
        db.session.query(
            func.date_format(
                Income.date, "%Y-%m"
            ).label("month"),
            func.sum(Income.amount).label("total")
        )
        .filter_by(user_id=g.user.id)
        .group_by("month")
        .order_by("month")
        .all()
    )

    monthly_expenses = (
        db.session.query(
            func.date_format(
                Expense.date, "%Y-%m"
            ).label("month"),
            func.sum(Expense.amount).label("total")
        )
        .filter_by(user_id=g.user.id)
        .group_by("month")
        .order_by("month")
        .all()
    )

    return render_template(
        "reports.html",
        total_income=total_income,
        total_expenses=total_expenses,
        balance=balance,
        expense_categories=expense_categories,
        monthly_income=monthly_income,
        monthly_expenses=monthly_expenses
    )


# --------------------------------------------------
# TRANSACTION HISTORY
# --------------------------------------------------

@app.route("/history")
@login_required
def history():

    selected_type = request.args.get("type", "All")

    search_query = request.args.get(
        "search", ""
    ).strip()

    transactions = []

    income_records = Income.query.filter_by(
        user_id=g.user.id
    ).all()

    for item in income_records:

        transactions.append({
            "type": "Income",
            "category": item.category,
            "description": item.description or "-",
            "amount": item.amount,
            "date": item.date,
            "id": item.id
        })

    expense_records = Expense.query.filter_by(
        user_id=g.user.id
    ).all()

    for item in expense_records:

        transactions.append({
            "type": "Expense",
            "category": item.category,
            "description": item.description or "-",
            "amount": item.amount,
            "date": item.date,
            "id": item.id
        })

    transactions.sort(
        key=lambda item: (item["date"], item["id"]),
        reverse=True
    )

    if selected_type in ["Income", "Expense"]:

        transactions = [
            item for item in transactions
            if item["type"] == selected_type
        ]

    else:
        selected_type = "All"

    if search_query:

        transactions = [
            item for item in transactions
            if (
                search_query.lower() in item["category"].lower()
                or search_query.lower() in item["description"].lower()
            )
        ]

    return render_template(
        "history.html",
        transactions=transactions,
        selected_type=selected_type,
        search_query=search_query
    )


# --------------------------------------------------
# CREATE TABLES AND RUN APP
# --------------------------------------------------

if __name__ == "__main__":

    with app.app_context():

        db.create_all()

        update_existing_database()

        print("Database tables checked successfully!")

    app.run(debug=True)
