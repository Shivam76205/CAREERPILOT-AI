from flask import Flask, render_template, request, redirect, url_for, session
import mysql.connector
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PyPDF2 import PdfReader
from google import genai
import os
from dotenv import load_dotenv
import json
import re
import time
import random


# ============================================================
# LOAD ENVIRONMENT VARIABLES
# ============================================================

load_dotenv()


# ============================================================
# FLASK APPLICATION
# ============================================================

app = Flask(__name__)

app.secret_key = os.getenv(
    "FLASK_SECRET_KEY",
    "careerpilot-secret-key"
)

app.config["UPLOAD_FOLDER"] = "uploads"


# ============================================================
# GEMINI CONFIGURATION - 5 KEY AUTOMATIC FAILOVER
# ============================================================

GEMINI_API_KEYS = [
    os.getenv("GEMINI_API_KEY_1"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
]

GEMINI_API_KEYS = [
    key.strip() for key in GEMINI_API_KEYS
    if key and key.strip()
]

if not GEMINI_API_KEYS:
    old_key = os.getenv("GEMINI_API_KEY")
    if old_key and old_key.strip():
        GEMINI_API_KEYS = [old_key.strip()]

if not GEMINI_API_KEYS:
    print("WARNING: No Gemini API keys are configured in .env")

gemini_clients = [
    genai.Client(api_key=key)
    for key in GEMINI_API_KEYS
]

GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_FALLBACK_MODEL = "gemini-3.5-flash-lite"

# Keys that have exhausted their daily quota.
exhausted_keys = set()


# ============================================================
# MYSQL CONFIGURATION
# ============================================================

MYSQL_HOST = os.getenv(
    "MYSQL_HOST",
    "127.0.0.1"
)

MYSQL_PORT = int(
    os.getenv(
        "MYSQL_PORT",
        "3306"
    )
)

MYSQL_USER = os.getenv(
    "MYSQL_USER",
    "root"
)

MYSQL_PASSWORD = os.getenv(
    "MYSQL_PASSWORD",
    "root"
)

MYSQL_DATABASE = os.getenv(
    "MYSQL_DATABASE",
    "CareerPilot"
)


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_db_connection():

    return mysql.connector.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE
    )


# ============================================================
# UPDATE DASHBOARD PROGRESS
# ============================================================

def update_progress(user_id, column, score):

    allowed_columns = [
        "resume_score",
        "interview_score",
        "aptitude_score",
        "skill_score"
    ]

    if column not in allowed_columns:
        return

    connection = None
    cursor = None

    try:

        connection = get_db_connection()
        cursor = connection.cursor()

        query = """
            SELECT id
            FROM progress
            WHERE user_id = %s
        """

        cursor.execute(
            query,
            (user_id,)
        )

        existing = cursor.fetchone()

        if existing:

            query = f"""
                UPDATE progress
                SET {column} = %s
                WHERE user_id = %s
            """

            cursor.execute(
                query,
                (
                    score,
                    user_id
                )
            )

        else:

            query = f"""
                INSERT INTO progress
                (user_id, {column})
                VALUES (%s, %s)
            """

            cursor.execute(
                query,
                (
                    user_id,
                    score
                )
            )

        connection.commit()

    except Exception as e:

        if connection:
            connection.rollback()

        print(
            f"Progress update error: {e}"
        )

    finally:

        if cursor:
            cursor.close()

        if connection:
            connection.close()


# ============================================================
# GEMINI HELPER - AUTOMATIC KEY FAILOVER
# ============================================================

def ask_gemini(prompt, max_retries=3):
    """Send a request to Gemini with automatic 5-key failover."""

    if not gemini_clients:
        raise RuntimeError(
            "No Gemini API keys are configured. "
            "Add GEMINI_API_KEY_1 through GEMINI_API_KEY_5 to your .env file."
        )

    last_error = None

    for key_index, client in enumerate(gemini_clients):

        if key_index in exhausted_keys:
            print(
                f"Gemini Key {key_index + 1} is exhausted. "
                "Trying the next key..."
            )
            continue

        key_abandoned = False

        for model in [GEMINI_MODEL, GEMINI_FALLBACK_MODEL]:

            for attempt in range(max_retries):

                try:
                    print(
                        f"Gemini Request | "
                        f"Key {key_index + 1}/{len(gemini_clients)} | "
                        f"Model: {model} | "
                        f"Attempt: {attempt + 1}/{max_retries}"
                    )

                    response = client.models.generate_content(
                        model=model,
                        contents=prompt
                    )

                    if not response:
                        raise RuntimeError("Gemini returned no response.")

                    if not response.text:
                        raise RuntimeError(
                            "Gemini returned an empty response."
                        )

                    print(
                        f"Gemini request successful using "
                        f"Key {key_index + 1} and {model}"
                    )

                    return response.text.strip()

                except Exception as e:

                    last_error = e
                    error_text = str(e)
                    error_upper = error_text.upper()

                    print("Gemini API Error:")
                    print(error_text)

                    invalid_key = (
                        "API_KEY_INVALID" in error_upper
                        or "API KEY NOT VALID" in error_upper
                    )

                    if invalid_key:
                        print(
                            f"Key {key_index + 1} is invalid. "
                            "Trying the next key..."
                        )
                        key_abandoned = True
                        break

                    daily_quota_error = (
                        "GENERATE_CONTENT_FREE_TIER_REQUESTS"
                        in error_upper
                        or
                        "GENERATEREQUESTSPERDAYPERPROJECTPERMODEL-FREETIER"
                        in error_upper
                        or
                        "GENERATEREQUESTSPERDAYPERMODEL-FREETIER"
                        in error_upper
                        or
                        "QUOTA EXCEEDED" in error_upper
                    )

                    if daily_quota_error:
                        exhausted_keys.add(key_index)
                        print(
                            f"Key {key_index + 1} daily quota exhausted. "
                            "Switching to the next key..."
                        )
                        key_abandoned = True
                        break

                    is_503 = (
                        "503" in error_text
                        or "UNAVAILABLE" in error_upper
                        or "HIGH DEMAND" in error_upper
                    )

                    is_429 = (
                        "429" in error_text
                        or "RESOURCE_EXHAUSTED" in error_upper
                    )

                    is_server_error = (
                        "500" in error_text
                        or "502" in error_text
                        or "504" in error_text
                        or "INTERNAL" in error_upper
                    )

                    if is_503 or is_429 or is_server_error:

                        if attempt < max_retries - 1:

                            wait_time = (
                                (2 ** attempt) * 2
                                + random.uniform(0, 1)
                            )

                            print(
                                f"Temporary Gemini error. "
                                f"Retrying in {wait_time:.1f} seconds..."
                            )

                            time.sleep(wait_time)
                            continue

                        print(
                            f"Key {key_index + 1} / {model} "
                            "failed after retries."
                        )
                        break

                    print(
                        f"Key {key_index + 1} failed. "
                        "Trying the next configured key..."
                    )
                    key_abandoned = True
                    break

            if key_abandoned:
                break

    if len(exhausted_keys) >= len(gemini_clients):
        raise RuntimeError(
            "All configured Gemini API keys have reached their "
            "available daily quota."
        )

    raise RuntimeError(
        "All configured Gemini API keys failed or are temporarily "
        f"unavailable. Last error: {last_error}"
    )


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return render_template(
        "index.html"
    )


# ============================================================
# REGISTER
# ============================================================

@app.route(
    "/register",
    methods=["GET", "POST"]
)
def register():

    if request.method == "POST":

        name = request.form.get(
            "name",
            ""
        ).strip()

        email = request.form.get(
            "email",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )


        if not name or not email or not password:

            return (
                "Please fill in all registration fields ❌"
            )


        hashed_password = generate_password_hash(
            password
        )


        connection = None
        cursor = None

        try:

            connection = get_db_connection()
            cursor = connection.cursor()

            query = """
                INSERT INTO users
                (name, email, password)
                VALUES (%s, %s, %s)
            """

            cursor.execute(
                query,
                (
                    name,
                    email,
                    hashed_password
                )
            )

            connection.commit()


        except mysql.connector.IntegrityError:

            return (
                "Email already registered ❌"
            )


        except Exception as e:

            return (
                f"Registration failed ❌<br><br>{e}"
            )


        finally:

            if cursor:
                cursor.close()

            if connection:
                connection.close()


        return redirect(
            url_for("login")
        )


    return render_template(
        "register.html"
    )


# ============================================================
# LOGIN
# ============================================================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )


        connection = None
        cursor = None

        try:

            connection = get_db_connection()

            cursor = connection.cursor(
                dictionary=True
            )


            query = """
                SELECT *
                FROM users
                WHERE email = %s
            """

            cursor.execute(
                query,
                (email,)
            )

            user = cursor.fetchone()


        except Exception as e:

            return (
                f"Database error ❌<br><br>{e}"
            )


        finally:

            if cursor:
                cursor.close()

            if connection:
                connection.close()


        if (
            user
            and check_password_hash(
                user["password"],
                password
            )
        ):

            session["user_id"] = user["id"]

            session["user_name"] = user["name"]

            session["user_email"] = user["email"]

            return redirect(
                url_for("dashboard")
            )


        return (
            "Invalid email or password ❌"
        )


    return render_template(
        "login.html"
    )


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("home")
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    connection = None
    cursor = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor(
            dictionary=True
        )


        query = """
            SELECT
                resume_score,
                interview_score,
                aptitude_score,
                skill_score
            FROM progress
            WHERE user_id = %s
        """

        cursor.execute(
            query,
            (
                session["user_id"],
            )
        )

        progress = cursor.fetchone()


    except Exception as e:

        return (
            f"Could not load dashboard ❌<br><br>{e}"
        )


    finally:

        if cursor:
            cursor.close()

        if connection:
            connection.close()


    if not progress:

        progress = {
            "resume_score": 0,
            "interview_score": 0,
            "aptitude_score": 0,
            "skill_score": 0
        }


    resume_score = (
        progress["resume_score"] or 0
    )

    interview_score = (
        progress["interview_score"] or 0
    )

    aptitude_score = (
        progress["aptitude_score"] or 0
    )

    skill_score = (
        progress["skill_score"] or 0
    )


    overall_score = round(
        (
            resume_score
            + interview_score
            + aptitude_score
            + skill_score
        ) / 4
    )


    return render_template(
        "dashboard.html",

        user_name=session.get(
            "user_name",
            "Student"
        ),

        resume_score=resume_score,

        interview_score=interview_score,

        aptitude_score=aptitude_score,

        skill_score=skill_score,

        overall_score=overall_score
    )


# ============================================================
# MOCK INTERVIEW
# ============================================================

@app.route(
    "/mock-interview",
    methods=["GET", "POST"]
)
def mock_interview():

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    if request.method == "POST":

        interview_type = request.form.get(
            "interview_type",
            "HR"
        )


        prompt = f"""
You are an AI interviewer conducting a {interview_type}
interview for a college student preparing for campus placements.

Generate ONE realistic {interview_type} interview question.

Rules:

- Ask only one question.
- Do not provide the answer.
- Keep the question clear.
- Suitable for a college student.
- Make it realistic for a placement interview.
"""


        try:

            question = ask_gemini(
                prompt
            )

        except Exception as e:

            return (
                "AI interview generation failed ❌"
                f"<br><br>{e}"
            )


        connection = None
        cursor = None

        try:

            connection = get_db_connection()

            cursor = connection.cursor()


            query = """
                INSERT INTO interviews
                (user_id, interview_type, question)
                VALUES (%s, %s, %s)
            """


            cursor.execute(
                query,
                (
                    session["user_id"],
                    interview_type,
                    question
                )
            )


            connection.commit()

            interview_id = cursor.lastrowid


        except Exception as e:

            if connection:
                connection.rollback()

            return (
                f"Could not save interview question ❌"
                f"<br><br>{e}"
            )


        finally:

            if cursor:
                cursor.close()

            if connection:
                connection.close()


        return render_template(
            "interview_question.html",

            question=question,

            interview_id=interview_id,

            interview_type=interview_type
        )


    return render_template(
        "mock_interview.html"
    )


# ============================================================
# SUBMIT INTERVIEW ANSWER
# ============================================================

@app.route(
    "/submit-answer",
    methods=["POST"]
)
def submit_answer():

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    interview_id = request.form.get(
        "interview_id"
    )

    answer = request.form.get(
        "answer",
        ""
    ).strip()


    if not interview_id:

        return (
            "Interview ID is missing ❌"
        )


    if not answer:

        return (
            "Please provide an answer ❌"
        )


    # --------------------------------------------------------
    # GET QUESTION
    # --------------------------------------------------------

    connection = None
    cursor = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor(
            dictionary=True
        )


        query = """
            SELECT question, interview_type
            FROM interviews
            WHERE id = %s
            AND user_id = %s
        """


        cursor.execute(
            query,
            (
                interview_id,
                session["user_id"]
            )
        )


        interview = cursor.fetchone()


    except Exception as e:

        return (
            f"Database error ❌<br><br>{e}"
        )


    finally:

        if cursor:
            cursor.close()

        if connection:
            connection.close()


    if not interview:

        return (
            "Interview question not found ❌"
        )


    question = interview["question"]

    interview_type = interview[
        "interview_type"
    ]


    # --------------------------------------------------------
    # GEMINI EVALUATION
    # --------------------------------------------------------

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


    try:

        feedback = ask_gemini(
            prompt
        )

    except Exception as e:

        return (
            "AI interview evaluation failed ❌"
            f"<br><br>{e}"
        )


    # --------------------------------------------------------
    # EXTRACT SCORE
    # --------------------------------------------------------

    score = 0


    score_match = re.search(
        r"SCORE\s*:\s*(\d+)",
        feedback,
        re.IGNORECASE
    )


    if score_match:

        try:

            score = int(
                score_match.group(1)
            )

        except ValueError:

            score = 0


    score = max(
        0,
        min(score, 100)
    )


    # --------------------------------------------------------
    # UPDATE DATABASE
    # --------------------------------------------------------

    connection = None
    cursor = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor()


        query = """
            UPDATE interviews

            SET answer = %s,
                score = %s,
                feedback = %s

            WHERE id = %s
            AND user_id = %s
        """


        cursor.execute(
            query,
            (
                answer,
                score,
                feedback,
                interview_id,
                session["user_id"]
            )
        )


        connection.commit()


    except Exception as e:

        if connection:
            connection.rollback()

        return (
            f"Could not save interview result ❌"
            f"<br><br>{e}"
        )


    finally:

        if cursor:
            cursor.close()

        if connection:
            connection.close()


    # --------------------------------------------------------
    # UPDATE PROGRESS
    # --------------------------------------------------------

    update_progress(
        session["user_id"],
        "interview_score",
        score
    )


    # --------------------------------------------------------
    # RESULT
    # --------------------------------------------------------

    return render_template(
        "interview_result.html",

        question=question,

        answer=answer,

        score=score,

        feedback=feedback,

        interview_type=interview_type
    )


# ============================================================
# RESUME ANALYZER
# ============================================================

@app.route(
    "/resume",
    methods=["GET", "POST"]
)
def resume():

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    if request.method == "GET":

        return render_template(
            "resume.html"
        )


    resume_file = request.files.get(
        "resume"
    )


    if (
        not resume_file
        or not resume_file.filename
    ):

        return (
            "Please select a resume PDF ❌"
        )


    if not resume_file.filename.lower().endswith(
        ".pdf"
    ):

        return (
            "Only PDF resumes are allowed ❌"
        )


    filename = secure_filename(
        resume_file.filename
    )


    if not filename:

        return (
            "Invalid file name ❌"
        )


    os.makedirs(
        app.config["UPLOAD_FOLDER"],
        exist_ok=True
    )


    file_path = os.path.join(
        app.config["UPLOAD_FOLDER"],
        filename
    )


    try:

        resume_file.save(
            file_path
        )

    except Exception as e:

        return (
            f"Could not save resume ❌"
            f"<br><br>{e}"
        )


    # --------------------------------------------------------
    # READ PDF
    # --------------------------------------------------------

    try:

        reader = PdfReader(
            file_path
        )

        resume_text = ""


        for page in reader.pages:

            text = page.extract_text()

            if text:

                resume_text += (
                    text + "\n"
                )


    except Exception as e:

        return (
            "Could not read the resume PDF ❌"
            f"<br><br>{e}"
        )


    if not resume_text.strip():

        return (
            "Could not extract text from this PDF ❌"
        )


    # --------------------------------------------------------
    # GEMINI ANALYSIS
    # --------------------------------------------------------

    prompt = f"""
You are an expert resume evaluator
specialized in college campus placements.

Analyze the following resume carefully.

Provide:

1. RESUME SCORE: [number from 0 to 100]
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


    try:

        analysis = ask_gemini(
            prompt
        )

    except Exception as e:

        return (
            "AI resume analysis failed ❌"
            f"<br><br>{e}"
        )


    # --------------------------------------------------------
    # EXTRACT SCORE
    # --------------------------------------------------------

    resume_score = 0


    score_match = re.search(
        r"RESUME\s*SCORE\s*:\s*(\d+)",
        analysis,
        re.IGNORECASE
    )


    if score_match:

        try:

            resume_score = int(
                score_match.group(1)
            )

        except ValueError:

            resume_score = 0


    resume_score = max(
        0,
        min(resume_score, 100)
    )


    # --------------------------------------------------------
    # SAVE RESUME
    # --------------------------------------------------------

    connection = None
    cursor = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor()


        query = """
            INSERT INTO resumes
            (user_id, file_name, file_path)
            VALUES (%s, %s, %s)
        """


        cursor.execute(
            query,
            (
                session["user_id"],
                filename,
                file_path
            )
        )


        connection.commit()


    except Exception as e:

        if connection:
            connection.rollback()

        return (
            f"Could not save resume information ❌"
            f"<br><br>{e}"
        )


    finally:

        if cursor:
            cursor.close()

        if connection:
            connection.close()


    # --------------------------------------------------------
    # UPDATE PROGRESS
    # --------------------------------------------------------

    update_progress(
        session["user_id"],
        "resume_score",
        resume_score
    )


    # --------------------------------------------------------
    # SHOW RESULT
    # --------------------------------------------------------

    return render_template(
        "resume_result.html",

        analysis=analysis,

        filename=filename
    )


# ============================================================
# CAREER ROADMAP
# ============================================================

@app.route(
    "/roadmap",
    methods=["GET", "POST"]
)
def roadmap():

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    if request.method == "POST":

        career_goal = request.form.get(
            "career_goal",
            ""
        ).strip()

        current_skills = request.form.get(
            "current_skills",
            ""
        ).strip()

        skill_level = request.form.get(
            "skill_level",
            ""
        ).strip()

        learning_time = request.form.get(
            "learning_time",
            ""
        ).strip()


        if not career_goal:

            return (
                "Please enter your career goal ❌"
            )


        prompt = f"""
You are an expert career mentor for college students
preparing for campus placements.

Create a practical and personalized career roadmap
for the student.

CAREER GOAL:
{career_goal}

CURRENT SKILLS:
{current_skills}

CURRENT SKILL LEVEL:
{skill_level}

AVAILABLE LEARNING TIME:
{learning_time}

Provide the roadmap in the following format:

1. CAREER OVERVIEW
Explain the career path briefly.

2. SKILLS TO LEARN
- Skill 1
- Skill 2
- Skill 3
- Skill 4

3. RECOMMENDED LEARNING ORDER
1. Step 1
2. Step 2
3. Step 3
4. Step 4

4. PROJECTS TO BUILD
- Project 1
- Project 2
- Project 3

5. INTERVIEW PREPARATION
- Topic 1
- Topic 2
- Topic 3

6. SUGGESTED TIMELINE
Create a realistic timeline based on the student's
current skill level and available learning time.

7. FINAL CAREER ADVICE
Give practical advice for becoming placement-ready.

Keep the roadmap realistic, clear and suitable
for a college student.
"""


        try:

            roadmap_result = ask_gemini(
                prompt
            )

        except Exception as e:

            return (
                "AI roadmap generation failed ❌"
                f"<br><br>{e}"
            )


        if not roadmap_result:

            return (
                "AI roadmap generation is "
                "temporarily unavailable ❌"
            )


        # ----------------------------------------------------
        # SAVE ROADMAP
        # ----------------------------------------------------

        connection = None
        cursor = None

        try:

            connection = get_db_connection()

            cursor = connection.cursor()


            query = """
                INSERT INTO roadmaps
                (user_id, career_goal, roadmap)
                VALUES (%s, %s, %s)
            """


            cursor.execute(
                query,
                (
                    session["user_id"],
                    career_goal,
                    roadmap_result
                )
            )


            connection.commit()


        except Exception as e:

            if connection:
                connection.rollback()

            return (
                f"Could not save roadmap ❌"
                f"<br><br>{e}"
            )


        finally:

            if cursor:
                cursor.close()

            if connection:
                connection.close()


        return render_template(
            "roadmap_result.html",

            career_goal=career_goal,

            current_skills=current_skills,

            skill_level=skill_level,

            learning_time=learning_time,

            roadmap_result=roadmap_result
        )


    return render_template(
        "roadmap.html"
    )


# ============================================================
# JOB MATCHER
# ============================================================

@app.route(
    "/job-matcher",
    methods=["GET", "POST"]
)
def job_matcher():

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    if request.method == "POST":

        resume_file = request.files.get(
            "resume"
        )


        job_description = request.form.get(
            "job_description",
            ""
        ).strip()


        if (
            not resume_file
            or not resume_file.filename
        ):

            return (
                "Please upload your resume PDF ❌"
            )


        if not resume_file.filename.lower().endswith(
            ".pdf"
        ):

            return (
                "Only PDF resumes are allowed ❌"
            )


        if not job_description:

            return (
                "Please enter the job description ❌"
            )


        filename = secure_filename(
            resume_file.filename
        )


        if not filename:

            return (
                "Invalid file name ❌"
            )


        os.makedirs(
            app.config["UPLOAD_FOLDER"],
            exist_ok=True
        )


        file_path = os.path.join(
            app.config["UPLOAD_FOLDER"],
            filename
        )


        try:

            resume_file.save(
                file_path
            )

        except Exception as e:

            return (
                f"Could not save resume ❌"
                f"<br><br>{e}"
            )


        # ----------------------------------------------------
        # READ PDF
        # ----------------------------------------------------

        try:

            reader = PdfReader(
                file_path
            )

            resume_text = ""


            for page in reader.pages:

                text = page.extract_text()

                if text:

                    resume_text += (
                        text + "\n"
                    )


        except Exception as e:

            return (
                "Could not read the resume PDF ❌"
                f"<br><br>{e}"
            )


        if not resume_text.strip():

            return (
                "Could not extract text from the resume ❌"
            )


        # ----------------------------------------------------
        # GEMINI ANALYSIS
        # ----------------------------------------------------

        prompt = f"""
You are an expert ATS and recruitment analyst.

Compare the student's resume with the given job description.

RESUME:
{resume_text}

JOB DESCRIPTION:
{job_description}

Provide the result in exactly this format:

MATCH SCORE: [number from 0 to 100]

MATCHED SKILLS:
- Skill 1
- Skill 2

MISSING SKILLS:
- Skill 1
- Skill 2

STRENGTHS:
- Point 1
- Point 2

IMPROVEMENT SUGGESTIONS:
- Point 1
- Point 2

FINAL RECOMMENDATION:
Explain whether the student is reasonably prepared
for this job and what they should improve.

Keep the analysis clear and suitable for a college student.
"""


        try:

            result = ask_gemini(
                prompt
            )

        except Exception as e:

            return (
                "AI job matching failed ❌"
                f"<br><br>{e}"
            )


        if not result:

            return (
                "AI job matching is "
                "temporarily unavailable ❌"
            )


        # ----------------------------------------------------
        # MATCH SCORE
        # ----------------------------------------------------

        match_score = 0


        score_match = re.search(
            r"MATCH\s*SCORE\s*:\s*(\d+)",
            result,
            re.IGNORECASE
        )


        if score_match:

            try:

                match_score = int(
                    score_match.group(1)
                )

            except ValueError:

                match_score = 0


        match_score = max(
            0,
            min(match_score, 100)
        )


        # ----------------------------------------------------
        # MISSING SKILLS
        # ----------------------------------------------------

        missing_skills = ""


        missing_match = re.search(
            r"MISSING\s*SKILLS\s*:\s*(.*?)(?=\n\s*STRENGTHS\s*:|\n\s*IMPROVEMENT\s*SUGGESTIONS\s*:)",
            result,
            re.IGNORECASE | re.DOTALL
        )


        if missing_match:

            missing_skills = (
                missing_match.group(1)
                .strip()
            )


        # ----------------------------------------------------
        # SUGGESTIONS
        # ----------------------------------------------------

        suggestions = ""


        suggestion_match = re.search(
            r"IMPROVEMENT\s*SUGGESTIONS\s*:\s*(.*?)(?=\n\s*FINAL\s*RECOMMENDATION\s*:)",
            result,
            re.IGNORECASE | re.DOTALL
        )


        if suggestion_match:

            suggestions = (
                suggestion_match.group(1)
                .strip()
            )


        # ----------------------------------------------------
        # SAVE JOB MATCH
        # ----------------------------------------------------

        connection = None
        cursor = None

        try:

            connection = get_db_connection()

            cursor = connection.cursor()


            query = """
                INSERT INTO job_matches
                (
                    user_id,
                    job_title,
                    job_description,
                    match_score,
                    missing_skills,
                    suggestions
                )
                VALUES (%s, %s, %s, %s, %s, %s)
            """


            cursor.execute(
                query,
                (
                    session["user_id"],
                    "Job Match Analysis",
                    job_description,
                    match_score,
                    missing_skills,
                    suggestions
                )
            )


            connection.commit()


        except Exception as e:

            if connection:
                connection.rollback()

            return (
                f"Could not save job match ❌"
                f"<br><br>{e}"
            )


        finally:

            if cursor:
                cursor.close()

            if connection:
                connection.close()


        # ----------------------------------------------------
        # RESULT
        # ----------------------------------------------------

        return render_template(
            "job_match_result.html",

            result=result,

            filename=filename,

            match_score=match_score,

            missing_skills=missing_skills,

            suggestions=suggestions
        )


    return render_template(
        "job_matcher.html"
    )


# ============================================================
# APTITUDE TEST
# ============================================================

@app.route(
    "/aptitude",
    methods=["GET", "POST"]
)
def aptitude():

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    if request.method == "GET":

        return render_template(
            "aptitude.html"
        )


    topic = request.form.get(
        "category"
    )

    difficulty = request.form.get(
        "difficulty"
    )


    if not topic or not difficulty:

        return (
            "Please select category and difficulty ❌"
        )


    prompt = f"""
You are an expert aptitude trainer for college placement preparation.

Generate EXACTLY 10 multiple-choice aptitude questions.

Topic: {topic}

Difficulty: {difficulty}

Requirements:

- Generate exactly 10 different questions.
- Suitable for college placement exams.
- Each question must have exactly 4 options.
- Options must be A, B, C and D.
- Give only one correct answer.
- Give a short explanation.
- Do not repeat questions.
- Do not add any extra text.

Return EXACTLY in this format:

QUESTION 1:
[question]

OPTIONS:
A) [option]
B) [option]
C) [option]
D) [option]

CORRECT ANSWER:
A

EXPLANATION:
[short explanation]

QUESTION 2:
[question]

OPTIONS:
A) [option]
B) [option]
C) [option]
D) [option]

CORRECT ANSWER:
B

EXPLANATION:
[short explanation]

Continue exactly the same format until QUESTION 10.
"""


    try:

        result = ask_gemini(
            prompt
        )

    except Exception as e:

        return (
            "AI aptitude test generation failed ❌"
            f"<br><br>{e}"
        )


    if not result:

        return (
            "AI aptitude test generation is "
            "temporarily unavailable ❌"
        )


    questions = []


    # --------------------------------------------------------
    # SPLIT QUESTIONS
    # --------------------------------------------------------

    question_blocks = re.split(
        r"QUESTION\s+\d+\s*:",
        result,
        flags=re.IGNORECASE
    )


    question_blocks = [
        block.strip()
        for block in question_blocks
        if block.strip()
    ]


    # --------------------------------------------------------
    # PARSE QUESTIONS
    # --------------------------------------------------------

    for block in question_blocks[:10]:

        question = ""

        options = {}

        correct_answer = ""

        explanation = ""


        # ----------------------------------------------------
        # QUESTION
        # ----------------------------------------------------

        question_match = re.search(
            r"^(.*?)(?=\n\s*OPTIONS\s*:)",
            block,
            re.IGNORECASE | re.DOTALL
        )


        if question_match:

            question = (
                question_match.group(1)
                .strip()
            )


        # ----------------------------------------------------
        # OPTIONS
        # ----------------------------------------------------

        option_matches = re.findall(
            r"^\s*([A-D])\)\s*(.+)$",
            block,
            re.IGNORECASE | re.MULTILINE
        )


        for letter, text in option_matches:

            options[
                letter.upper()
            ] = text.strip()


        # ----------------------------------------------------
        # CORRECT ANSWER
        # ----------------------------------------------------

        answer_match = re.search(
            r"CORRECT\s+ANSWER\s*:\s*([A-D])",
            block,
            re.IGNORECASE
        )


        if answer_match:

            correct_answer = (
                answer_match.group(1)
                .upper()
                .strip()
            )


        # ----------------------------------------------------
        # EXPLANATION
        # ----------------------------------------------------

        explanation_match = re.search(
            r"EXPLANATION\s*:\s*(.*)",
            block,
            re.IGNORECASE | re.DOTALL
        )


        if explanation_match:

            explanation = (
                explanation_match.group(1)
                .strip()
            )


        # ----------------------------------------------------
        # VALIDATION
        # ----------------------------------------------------

        if (
            question
            and len(options) == 4
            and all(
                letter in options
                for letter in [
                    "A",
                    "B",
                    "C",
                    "D"
                ]
            )
            and correct_answer in [
                "A",
                "B",
                "C",
                "D"
            ]
            and explanation
        ):

            questions.append({

                "question": question,

                "options": options,

                "correct_answer": correct_answer,

                "explanation": explanation
            })


    # --------------------------------------------------------
    # EXACTLY 10 QUESTIONS
    # --------------------------------------------------------

    if len(questions) != 10:

        return (
            "AI generated an invalid aptitude test ❌"
            "<br><br>"
            f"Generated questions: "
            f"{len(questions)} / 10"
            "<br>Please try again."
        )


    # --------------------------------------------------------
    # SAVE QUESTIONS
    # --------------------------------------------------------

    connection = None
    cursor = None

    try:

        connection = get_db_connection()

        cursor = connection.cursor()


        query = """
            INSERT INTO aptitude_results
            (
                user_id,
                topic,
                difficulty,
                question,
                options,
                correct_answer,
                explanation
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """


        for q in questions:

            cursor.execute(
                query,
                (
                    session["user_id"],

                    topic,

                    difficulty,

                    q["question"],

                    json.dumps(
                        q["options"]
                    ),

                    q["correct_answer"],

                    q["explanation"]
                )
            )


            q["id"] = cursor.lastrowid


        connection.commit()


    except Exception as e:

        if connection:
            connection.rollback()

        return (
            "Could not save aptitude test ❌"
            f"<br><br>{e}"
        )


    finally:

        if cursor:
            cursor.close()

        if connection:
            connection.close()


    return render_template(
        "aptitude_test.html",

        questions=questions,

        topic=topic,

        difficulty=difficulty
    )


# ============================================================
# SUBMIT APTITUDE TEST
# ============================================================

@app.route(
    "/submit-aptitude-test",
    methods=["POST"]
)
def submit_aptitude_test():

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    user_id = session["user_id"]


    connection = None
    cursor = None


    try:

        connection = get_db_connection()

        cursor = connection.cursor(
            dictionary=True
        )


        query = """
            SELECT
                id,
                topic,
                difficulty,
                question,
                options,
                correct_answer,
                explanation
            FROM aptitude_results
            WHERE user_id = %s
            ORDER BY id DESC
            LIMIT 10
        """


        cursor.execute(
            query,
            (user_id,)
        )


        questions = cursor.fetchall()


    except Exception as e:

        return (
            f"Could not load aptitude test ❌"
            f"<br><br>{e}"
        )


    finally:

        if cursor:
            cursor.close()

        if connection:
            connection.close()


    if len(questions) != 10:

        return (
            "Aptitude test questions not found ❌"
        )


    questions.reverse()


    score_count = 0

    results = []


    connection = None
    cursor = None


    try:

        connection = get_db_connection()

        cursor = connection.cursor()


        for index, q in enumerate(
            questions,
            start=1
        ):

            selected_answer = request.form.get(
                f"answer_{index}"
            )


            if not selected_answer:

                return (
                    f"Please answer Question "
                    f"{index} before submitting ❌"
                )


            selected_answer = (
                selected_answer
                .upper()
                .strip()
            )


            correct_answer = (
                q["correct_answer"]
                .upper()
                .strip()
            )


            if selected_answer == correct_answer:

                score_count += 1

                question_score = 100

            else:

                question_score = 0


            update_query = """
                UPDATE aptitude_results
                SET selected_answer = %s,
                    score = %s
                WHERE id = %s
                AND user_id = %s
            """


            cursor.execute(
                update_query,
                (
                    selected_answer,

                    question_score,

                    q["id"],

                    user_id
                )
            )


            results.append({

                "question": q["question"],

                "selected_answer":
                    selected_answer,

                "correct_answer":
                    correct_answer,

                "explanation":
                    q["explanation"]
            })


        final_score = round(
            (score_count / 10) * 100
        )


        connection.commit()


    except Exception as e:

        if connection:
            connection.rollback()

        return (
            "Could not submit aptitude test ❌"
            f"<br><br>{e}"
        )


    finally:

        if cursor:
            cursor.close()

        if connection:
            connection.close()


    update_progress(
        user_id,
        "aptitude_score",
        final_score
    )


    return render_template(
        "aptitude_answer_result.html",

        results=results,

        score_count=score_count,

        total_questions=10,

        final_score=final_score,

        topic=questions[0]["topic"],

        difficulty=questions[0]["difficulty"]
    )


# ============================================================
# SKILL GAP ANALYZER
# ============================================================

@app.route(
    "/skill-gap",
    methods=["GET", "POST"]
)
def skill_gap():

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    if request.method == "POST":

        target_role = request.form.get(
            "target_role",
            ""
        ).strip()

        current_skills = request.form.get(
            "current_skills",
            ""
        ).strip()

        experience = request.form.get(
            "experience",
            ""
        ).strip()


        if not target_role:

            return (
                "Please enter your target role ❌"
            )


        prompt = f"""
You are an expert career and skill-gap analyst
helping college students prepare for campus placements.

Analyze the student's current skills against their target career.

TARGET CAREER:
{target_role}

CURRENT SKILLS:
{current_skills}

EXPERIENCE LEVEL:
{experience}

Provide the analysis in exactly this format:

SKILL GAP SCORE: [number from 0 to 100]

CURRENT STRENGTHS:
- Point 1
- Point 2
- Point 3

MISSING SKILLS:
- Skill 1
- Skill 2
- Skill 3

PRIORITY SKILLS:
- Skill 1
- Skill 2
- Skill 3

LEARNING RECOMMENDATIONS:
- Recommendation 1
- Recommendation 2
- Recommendation 3

PROJECT RECOMMENDATIONS:
- Project 1
- Project 2

PLACEMENT ADVICE:
Give practical advice for becoming placement-ready.

Keep the analysis clear, realistic and suitable
for a college student.
"""


        try:

            result = ask_gemini(
                prompt
            )

        except Exception as e:

            return (
                "AI skill gap analysis failed ❌"
                f"<br><br>{e}"
            )


        if not result:

            return (
                "AI skill gap analysis is "
                "temporarily unavailable ❌"
            )


        # ----------------------------------------------------
        # EXTRACT SCORE
        # ----------------------------------------------------

        skill_gap_score = 0


        score_match = re.search(
            r"SKILL\s*GAP\s*SCORE\s*:\s*(\d+)",
            result,
            re.IGNORECASE
        )


        if score_match:

            try:

                skill_gap_score = int(
                    score_match.group(1)
                )

            except ValueError:

                skill_gap_score = 0


        skill_gap_score = max(
            0,
            min(skill_gap_score, 100)
        )


        # ----------------------------------------------------
        # MISSING SKILLS
        # ----------------------------------------------------

        missing_skills = ""


        missing_match = re.search(
            r"MISSING\s*SKILLS\s*:\s*(.*?)(?=\n\s*PRIORITY\s*SKILLS\s*:)",
            result,
            re.IGNORECASE | re.DOTALL
        )


        if missing_match:

            missing_skills = (
                missing_match.group(1)
                .strip()
            )


        # ----------------------------------------------------
        # RECOMMENDATIONS
        # ----------------------------------------------------

        recommendations = ""


        recommendation_match = re.search(
            r"LEARNING\s*RECOMMENDATIONS\s*:\s*(.*?)(?=\n\s*PROJECT\s*RECOMMENDATIONS\s*:)",
            result,
            re.IGNORECASE | re.DOTALL
        )


        if recommendation_match:

            recommendations = (
                recommendation_match.group(1)
                .strip()
            )


        # ----------------------------------------------------
        # SAVE RESULT
        # ----------------------------------------------------

        connection = None
        cursor = None


        try:

            connection = get_db_connection()

            cursor = connection.cursor()


            query = """
                INSERT INTO skill_gap_results
                (
                    user_id,
                    target_role,
                    current_skills,
                    experience,
                    skill_gap_score,
                    missing_skills,
                    recommendations,
                    result
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """


            cursor.execute(
                query,
                (
                    session["user_id"],

                    target_role,

                    current_skills,

                    experience,

                    skill_gap_score,

                    missing_skills,

                    recommendations,

                    result
                )
            )


            connection.commit()


        except Exception as e:

            if connection:
                connection.rollback()

            return (
                f"Could not save skill gap result ❌"
                f"<br><br>{e}"
            )


        finally:

            if cursor:
                cursor.close()

            if connection:
                connection.close()


        # ----------------------------------------------------
        # UPDATE PROGRESS
        # ----------------------------------------------------

        update_progress(
            session["user_id"],
            "skill_score",
            skill_gap_score
        )


        # ----------------------------------------------------
        # SHOW RESULT
        # ----------------------------------------------------

        return render_template(
            "skill_gap_result.html",

            target_role=target_role,

            current_skills=current_skills,

            experience=experience,

            result=result
        )


    return render_template(
        "skill_gap.html"
    )


# ============================================================
# RECRUITER SIMULATION
# ============================================================

@app.route(
    "/recruiter",
    methods=["GET", "POST"]
)
def recruiter():

    if "user_id" not in session:

        return redirect(
            url_for("login")
        )


    if request.method == "POST":

        career_role = request.form.get(
            "career_role",
            ""
        ).strip()

        experience = request.form.get(
            "experience",
            ""
        ).strip()


        if not career_role:

            return (
                "Please enter your career role ❌"
            )


        prompt = f"""
You are an AI recruiter conducting a hiring simulation
for a college student preparing for campus placements.

Target Role:
{career_role}

Student Experience:
{experience}

Conduct a short recruiter evaluation.

Provide the result in exactly this format:

HIRING READINESS SCORE: [number from 0 to 100]

TECHNICAL READINESS:
Give a short evaluation.

COMMUNICATION:
Give a short evaluation.

STRENGTHS:
- Point 1
- Point 2

AREAS TO IMPROVE:
- Point 1
- Point 2

RECRUITER FEEDBACK:
Give practical feedback from the perspective of a recruiter.

HIRING RECOMMENDATION:
Choose one:
- Strong Candidate
- Potential Candidate
- Needs Improvement

NEXT STEPS:
- Step 1
- Step 2
- Step 3

Keep the evaluation realistic and suitable
for a college placement candidate.
"""


        try:

            result = ask_gemini(
                prompt
            )

        except Exception as e:

            return (
                "AI recruiter simulation failed ❌"
                f"<br><br>{e}"
            )


        if not result:

            return (
                "AI recruiter simulation is "
                "temporarily unavailable ❌"
            )


        # ----------------------------------------------------
        # EXTRACT SCORE
        # ----------------------------------------------------

        hiring_score = 0


        score_match = re.search(
            r"HIRING\s*READINESS\s*SCORE\s*:\s*(\d+)",
            result,
            re.IGNORECASE
        )


        if score_match:

            try:

                hiring_score = int(
                    score_match.group(1)
                )

            except ValueError:

                hiring_score = 0


        hiring_score = max(
            0,
            min(hiring_score, 100)
        )


        # ----------------------------------------------------
        # EXTRACT RECOMMENDATION
        # ----------------------------------------------------

        recommendation = ""


        recommendation_match = re.search(
            r"HIRING\s*RECOMMENDATION\s*:\s*(.*?)(?=\n\s*NEXT\s*STEPS\s*:)",
            result,
            re.IGNORECASE | re.DOTALL
        )


        if recommendation_match:

            recommendation = (
                recommendation_match.group(1)
                .strip()
            )


        # ----------------------------------------------------
        # SAVE RESULT
        # ----------------------------------------------------

        connection = None
        cursor = None


        try:

            connection = get_db_connection()

            cursor = connection.cursor()


            query = """
                INSERT INTO recruiter_results
                (
                    user_id,
                    career_role,
                    experience,
                    hiring_score,
                    recommendation,
                    result
                )
                VALUES (%s, %s, %s, %s, %s, %s)
            """


            cursor.execute(
                query,
                (
                    session["user_id"],

                    career_role,

                    experience,

                    hiring_score,

                    recommendation,

                    result
                )
            )


            connection.commit()


        except Exception as e:

            if connection:
                connection.rollback()

            return (
                f"Could not save recruiter result ❌"
                f"<br><br>{e}"
            )


        finally:

            if cursor:
                cursor.close()

            if connection:
                connection.close()


        # ----------------------------------------------------
        # SHOW RESULT
        # ----------------------------------------------------

        return render_template(
            "recruiter_result.html",

            career_role=career_role,

            experience=experience,

            result=result
        )


    return render_template(
        "recruiter.html"
    )


# ============================================================
# RUN APPLICATION
# ============================================================

if __name__ == "__main__":

    print("=" * 60)
    print("CareerPilot AI")
    print("=" * 60)

    print(
        f"Gemini Primary Model: {GEMINI_MODEL}"
    )

    print(
        f"Gemini Fallback Model: {GEMINI_FALLBACK_MODEL}"
    )

    print(
        f"MySQL Database: {MYSQL_DATABASE}"
    )

    print("=" * 60)

    app.run(
        debug=True
    )