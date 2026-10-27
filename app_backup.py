from flask import Flask, render_template, request, redirect, url_for
import mysql.connector
from werkzeug.security import generate_password_hash, check_password_hash
import os
from werkzeug.utils import secure_filename
from PyPDF2 import PdfReader
from google import genai


app = Flask(__name__)

# =========================
# UPLOAD CONFIGURATION
# =========================

app.config["UPLOAD_FOLDER"] = "uploads"


# =========================
# GEMINI CONFIGURATION
# =========================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(
    api_key=GEMINI_API_KEY
)


# =========================
# MYSQL CONNECTION
# =========================

def get_db_connection():
    return mysql.connector.connect(
        host="127.0.0.1",
        port=3306,
        user="root",
        password="@shivam160207",
        database="CareerPilot"
    )


# =========================
# HOME
# =========================

@app.route("/")
def home():
    return render_template("index.html")


# =========================
# REGISTER
# =========================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = request.form["name"]
        email = request.form["email"]
        password = request.form["password"]

        hashed_password = generate_password_hash(password)

        connection = get_db_connection()
        cursor = connection.cursor()

        query = """
        INSERT INTO users (name, email, password)
        VALUES (%s, %s, %s)
        """

        cursor.execute(
            query,
            (name, email, hashed_password)
        )

        connection.commit()

        cursor.close()
        connection.close()

        return "Account Created Successfully! ✅"

    return render_template("register.html")


# =========================
# LOGIN
# =========================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form["email"]
        password = request.form["password"]

        connection = get_db_connection()

        cursor = connection.cursor(dictionary=True)

        query = """
        SELECT * FROM users
        WHERE email = %s
        """

        cursor.execute(query, (email,))

        user = cursor.fetchone()

        cursor.close()
        connection.close()

        if user and check_password_hash(
            user["password"],
            password
        ):
            return redirect(url_for("dashboard"))

        return "Invalid email or password ❌"

    return render_template("login.html")


# =========================
# DASHBOARD
# =========================

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")

# =========================
# Mock_Interview
# =========================
@app.route("/mock-interview", methods=["GET", "POST"])
def mock_interview():

    if request.method == "POST":

        interview_type = request.form["interview_type"]

        prompt = f"""
You are an AI interviewer conducting a {interview_type} interview
for a college student preparing for campus placements.

Generate ONE realistic {interview_type} interview question.

Rules:
- Ask only one question.
- Do not provide the answer.
- Keep the question clear and suitable for a college student.
"""

        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt
        )

        question = response.text.strip()

        # Save question in database
        connection = get_db_connection()
        cursor = connection.cursor()

        query = """
        INSERT INTO interviews
        (user_id, interview_type, question)
        VALUES (%s, %s, %s)
        """

        user_id = 1

        cursor.execute(
            query,
            (user_id, interview_type, question)
        )

        connection.commit()

        interview_id = cursor.lastrowid

        cursor.close()
        connection.close()

        return render_template(
            "interview_question.html",
            question=question,
            interview_id=interview_id,
            interview_type=interview_type
        )

    return render_template("mock_interview.html")

# =========================
# Mock_Interview_Answer
# =========================
@app.route("/submit-answer", methods=["POST"])
def submit_answer():

    interview_id = request.form["interview_id"]
    answer = request.form["answer"]

    # Get question from database
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    query = """
    SELECT question, interview_type
    FROM interviews
    WHERE id = %s
    """

    cursor.execute(query, (interview_id,))
    interview = cursor.fetchone()

    cursor.close()
    connection.close()

    if not interview:
        return "Interview question not found ❌"

    question = interview["question"]
    interview_type = interview["interview_type"]

    # Evaluate answer using Gemini
    prompt = f"""
You are an expert {interview_type} interviewer.

Evaluate the student's answer.

Interview Question:
{question}

Student Answer:
{answer}

Give your evaluation in exactly this format:

SCORE: [number from 0 to 100]

STRENGTHS:
- Point 1
- Point 2

WEAKNESSES:
- Point 1
- Point 2

FEEDBACK:
Write a short and useful explanation.

BETTER ANSWER:
Provide an improved example answer.
"""

    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt
    )

    feedback = response.text.strip()

    # Extract score
    score = 0

    for line in feedback.splitlines():

        if line.upper().startswith("SCORE:"):

            try:
                score = int(
                    line.split(":", 1)[1].strip()
                )
            except ValueError:
                score = 0

            break

    # Keep score between 0 and 100
    score = max(0, min(score, 100))

    # Save answer, score and feedback
    connection = get_db_connection()
    cursor = connection.cursor()

    query = """
    UPDATE interviews
    SET answer = %s,
        score = %s,
        feedback = %s
    WHERE id = %s
    """

    cursor.execute(
        query,
        (answer, score, feedback, interview_id)
    )

    connection.commit()

    cursor.close()
    connection.close()

    return render_template(
        "interview_result.html",
        question=question,
        answer=answer,
        score=score,
        feedback=feedback,
        interview_type=interview_type
    )

# =========================
# RESUME ANALYZER
# =========================

@app.route("/resume", methods=["GET", "POST"])
def resume():

    if request.method == "POST":

        # Get uploaded file
        resume_file = request.files.get("resume")

        # Check file
        if resume_file and resume_file.filename:

            # Safe filename
            filename = secure_filename(
                resume_file.filename
            )

            # Create uploads folder
            os.makedirs(
                app.config["UPLOAD_FOLDER"],
                exist_ok=True
            )

            # File path
            file_path = os.path.join(
                app.config["UPLOAD_FOLDER"],
                filename
            )

            # Save PDF
            resume_file.save(file_path)

            # =========================
            # EXTRACT PDF TEXT
            # =========================

            reader = PdfReader(file_path)

            resume_text = ""

            for page in reader.pages:

                text = page.extract_text()

                if text:
                    resume_text += text

            # =========================
            # GEMINI ANALYSIS
            # =========================

            prompt = f"""
You are an expert resume evaluator
specialized in college campus placements.

Analyze the following resume carefully.

Provide the following sections:

1. Resume Score out of 100
2. Overall Status
3. Strengths
4. Weaknesses
5. Missing Skills
6. Suggested Skills to Learn
7. Improvement Suggestions

Focus on:
- ATS compatibility
- Technical skills
- Projects
- Education
- Professional formatting
- Communication
- Placement readiness

Keep the explanation clear and easy to understand
for a college student.

Resume:

{resume_text}
"""

            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=prompt
            )

            analysis = response.text

            # =========================
            # SAVE RESUME IN MYSQL
            # =========================

            connection = get_db_connection()

            cursor = connection.cursor()

            query = """
            INSERT INTO resumes
            (user_id, file_name, file_path)
            VALUES (%s, %s, %s)
            """

            # Temporary user ID
            user_id = 1

            cursor.execute(
                query,
                (
                    user_id,
                    filename,
                    file_path
                )
            )

            connection.commit()

            cursor.close()
            connection.close()

            # =========================
            # SHOW RESULT PAGE
            # =========================

            return render_template(
                "resume_result.html",
                analysis=analysis,
                filename=filename
            )

    # GET request
    return render_template("resume.html")


# =========================
# RUN APPLICATION
# =========================

if __name__ == "__main__":
    app.run(debug=True)