from flask import Flask, request, render_template, redirect, url_for, send_file, jsonify, Response
import csv
import os
import sendgrid
from sendgrid.helpers.mail import Mail, Email, To, Content, Attachment, FileContent, FileName, FileType, Disposition, ContentId
import base64
import random
import time
import datetime
import json
import uuid
import hashlib
from email.utils import formatdate, make_msgid
import re
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("email_sender.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
UPLOAD_FOLDER = "templates"
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure folders exist
for folder in [UPLOAD_FOLDER]:
    if not os.path.exists(folder):
        os.makedirs(folder)

# Configuration for SendGrid
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', 'SG.your-api-key')  # Replace with your SendGrid API key or set in environment
EMAIL_ADDRESS = "asmarahmadbutt786@gmail.com"  # Your email address
SENDER_NAME = "RoutePricing Support"  # Your sender name

# Path to store data
PROGRESS_FILE = "data/email_progress.json"
SENT_EMAILS_FILE = "data/sent_emails.json"
EMAIL_HISTORY_FILE = "data/email_history.json"
UNSUBSCRIBE_LIST_FILE = "data/unsubscribed.json"
SENDING_SCHEDULE_FILE = "data/sending_schedule.json"  # New file to track progressive sending schedule

# Create data directory if it doesn't exist
if not os.path.exists("data"):
    os.makedirs("data")

# Email sending limits - UPDATED VALUES
MONTHLY_EMAIL_CAP = 60000
MAX_DAILY_EMAILS = 2000  # Increased from 1000 to 2000 as requested
DEFAULT_BATCH_SIZE = 50  # Default batch size to process at once
MIN_DELAY = 5  # Minimum delay between emails in seconds
MAX_DELAY = 15  # Maximum delay between emails in seconds
BATCH_PAUSE = 60  # Pause between batches in seconds to avoid spam detection

# Progressive sending schedule (emails per week)
PROGRESSIVE_SCHEDULE = {
    1: 700,    # Week 1: 700 emails/week (100 per day)
    2: 1400,   # Week 2: 1400 emails/week (200 per day)
    3: 2800,   # Week 3: 2800 emails/week (400 per day)
    4: 5600,   # Week 4: 5600 emails/week (800 per day)
    5: 11200,  # Week 5: 11200 emails/week (if needed and within monthly limit)
}

# Regular expression for validating email addresses
EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')

# Load sending schedule
def load_sending_schedule():
    """
    Load the progressive sending schedule data
    """
    if os.path.exists(SENDING_SCHEDULE_FILE):
        try:
            with open(SENDING_SCHEDULE_FILE, 'r') as file:
                return json.load(file)
        except json.JSONDecodeError:
            logger.warning(f"Error decoding {SENDING_SCHEDULE_FILE}. Creating new schedule.")
    
    # Return a new schedule if none exists
    # Start date is today, week 1
    start_date = str(datetime.date.today())
    return {
        "start_date": start_date,
        "current_week": 1,
        "weekly_sent": 0,
        "last_sent_date": start_date
    }

# Save sending schedule
def save_sending_schedule(schedule):
    """
    Save the progressive sending schedule data
    """
    with open(SENDING_SCHEDULE_FILE, 'w') as file:
        json.dump(schedule, file)

# Calculate current week and update schedule
def update_sending_schedule(schedule):
    """
    Update the sending schedule based on the current date
    """
    start_date = datetime.datetime.strptime(schedule["start_date"], '%Y-%m-%d').date()
    current_date = datetime.date.today()
    
    # Calculate days since start
    days_since_start = (current_date - start_date).days
    
    # Calculate current week (1-indexed)
    current_week = (days_since_start // 7) + 1
    
    # If it's a new week, reset the weekly counter
    if current_week > schedule["current_week"]:
        logger.info(f"Starting new week: Week {current_week}")
        schedule["current_week"] = current_week
        schedule["weekly_sent"] = 0
    
    # If it's a new day, update the last sent date
    if str(current_date) != schedule["last_sent_date"]:
        schedule["last_sent_date"] = str(current_date)
    
    return schedule

# Calculate daily email limit
# Calculate daily email limit
def calculate_daily_limit(progress, schedule):
    """
    Calculate how many emails we can send today based on:
    - Progressive sending schedule
    - Weekly sending cap
    - Daily email limit based on the weekly limit divided by 7
    - How many emails were already sent today
    - Monthly cap considerations
    """
    # Check if it's a new day
    today = str(datetime.date.today())
    if today != progress.get('last_sent_date'):
        # Reset daily counter for a new day
        progress['emails_sent_today'] = 0
        progress['last_sent_date'] = today
    
    # Check if it's a new month
    current_month = datetime.date.today().replace(day=1)
    last_sent_date = datetime.datetime.strptime(progress['last_sent_date'], '%Y-%m-%d').date()
    last_sent_month = last_sent_date.replace(day=1)
    
    if current_month > last_sent_month:
        # Reset monthly counter for a new month
        progress['emails_sent_this_month'] = 0
    
    # Get the current week and determine weekly limit
    current_week = schedule["current_week"]
    weekly_limit = PROGRESSIVE_SCHEDULE.get(current_week, PROGRESSIVE_SCHEDULE[5])  # Default to week 5's limit if beyond
    
    # Calculate daily limit based on the weekly limit (dividing by 7 days)
    # This implements your desired limits of 100/day for week 1, 200/day for week 2, etc.
    daily_limit = weekly_limit // 7
    
    # Calculate how many more emails we can send this week
    weekly_remaining = weekly_limit - schedule["weekly_sent"]
    
    # Calculate remaining emails we can send today
    remaining_daily = daily_limit - progress['emails_sent_today']
    remaining_monthly = MONTHLY_EMAIL_CAP - progress['emails_sent_this_month']
    
    # Return the minimum of all constraints
    return min(remaining_daily, weekly_remaining, remaining_monthly)

# Load unsubscribed emails
def load_unsubscribed():
    if os.path.exists(UNSUBSCRIBE_LIST_FILE):
        try:
            with open(UNSUBSCRIBE_LIST_FILE, 'r') as file:
                return set(json.load(file))
        except json.JSONDecodeError:
            logger.warning(f"Error decoding {UNSUBSCRIBE_LIST_FILE}. Creating new unsubscribe list.")
    return set()

# Save unsubscribed emails
def save_unsubscribed(unsubscribed_list):
    with open(UNSUBSCRIBE_LIST_FILE, 'w') as file:
        json.dump(list(unsubscribed_list), file)

# Load sent emails
def load_sent_emails():
    if os.path.exists(SENT_EMAILS_FILE):
        try:
            with open(SENT_EMAILS_FILE, 'r') as file:
                return set(json.load(file))
        except json.JSONDecodeError:
            logger.warning(f"Error decoding {SENT_EMAILS_FILE}. Creating new sent emails list.")
    return set()

# Save sent emails
def save_sent_emails(sent_emails):
    with open(SENT_EMAILS_FILE, 'w') as file:
        json.dump(list(sent_emails), file)

# Load email history
def load_email_history():
    if os.path.exists(EMAIL_HISTORY_FILE):
        try:
            with open(EMAIL_HISTORY_FILE, 'r') as file:
                return json.load(file)
        except json.JSONDecodeError:
            logger.warning(f"Error decoding {EMAIL_HISTORY_FILE}. Creating new email history.")
    return {}

# Save email history
def save_email_history(history):
    with open(EMAIL_HISTORY_FILE, 'w') as file:
        json.dump(history, file, indent=4)

# Load email progress
def load_email_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as file:
                return json.load(file)
        except json.JSONDecodeError:
            logger.warning(f"Error decoding {PROGRESS_FILE}. Creating new progress file.")
    return {"last_sent_index": 0, "emails_sent_this_month": 0, "emails_sent_today": 0, "last_sent_date": str(datetime.date.today())}

# Save email progress
def save_email_progress(progress):
    with open(PROGRESS_FILE, 'w') as file:
        json.dump(progress, file)

# Create a unique tracking ID for each email
def generate_tracking_id(email):
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    unique_id = f"{email}-{timestamp}-{uuid.uuid4().hex[:8]}"
    hashed_id = hashlib.sha256(unique_id.encode()).hexdigest()[:16]
    return hashed_id

# Personalize email content to avoid spam filters
def personalize_email(to_email, tracking_id, host_url="http://localhost:5000"):
    """
    Create personalized email content with tracking pixel and click tracking
    
    Args:
        to_email (str): Recipient email address
        tracking_id (str): Unique tracking ID for this email
        host_url (str): Base URL for the application (default: http://localhost:5000)
    """
    # Get the username from the email for personalization
    username = to_email.split('@')[0].replace(".", " ").title()
    
    # Create subject with slight randomization to avoid spam filters
    subject_options = [
        "Set delivery cost per km/mile for your store",
        f"Distance-based pricing for your {username.split()[0]} business",
        "WooCommerce delivery pricing by distance",
        "Calculate delivery fees automatically by distance"
    ]
    
    subject = random.choice(subject_options)
    
    # Create tracking URLs using the host_url
    tracking_pixel_url = f"{host_url}/track/{tracking_id}"
    unsubscribe_url = f"{host_url}/unsubscribe?email={to_email}&id={tracking_id}"
    click_tracking_url = f"{host_url}/click/{tracking_id}?url=https://wordpress.org/plugins/calculate-prices-based-on-distance-for-woocommerce/"
    
    # Create HTML template with proper unsubscribe link and tracking pixel
    html_content = f"""
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
      </head>
      <body style="font-family: Arial, sans-serif; background-color: #f9f9f9; padding: 20px; color: #333; line-height: 1.6;">
        <div style="max-width: 600px; margin: auto; background: #fff; border: 1px solid #ddd; padding: 25px; border-radius: 6px;">

          <p style="text-align: center;">
            <img src="https://www.routepricing.com/wp-content/uploads/2025/04/cropped-route-pricing-hd-logo-192x192.png" alt="RoutePricing Logo" style="max-width: 100px; margin-bottom: 20px;">
          </p>

          <p>Hi {username},</p>

          <h2 style="color: #1a73e8; margin-top: 15px;">📍 Add Real-Time Distance-Based Delivery Pricing to Your WooCommerce Store</h2>

          <p>
            Our free plugin for WooCommerce lets you automatically calculate delivery costs per <strong>mile or kilometer</strong>, based on the <strong>real-time distance</strong> between your store and your customer — powered by the <strong>Google Maps API</strong>.
          </p>

          <p>
            It's perfect for restaurants, couriers, service businesses, or anyone who wants to set accurate delivery fees that scale with distance.
          </p>

          <div style="background-color: #f5f9ff; padding: 15px; border-radius: 5px; margin: 20px 0;">
            <p style="margin-top: 0;">
              ✅ Easy setup<br />
              ✅ Real-time calculation at checkout<br />
              ✅ Fully free on WordPress.org
            </p>
          </div>

          <p style="text-align: center; margin: 25px 0;">
            <a href="{click_tracking_url}" 
               style="background-color: #1a73e8; color: white; padding: 12px 25px; text-decoration: none; border-radius: 4px; font-weight: bold; display: inline-block;">
              Get the plugin now
            </a>
          </p>

          <p>
            Let me know if you have any questions about setting up distance-based pricing for your store!
          </p>

          <p>
            Best regards,<br />
            The RoutePricing Team
          </p>

          <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0 20px;" />

          <p style="font-size: 12px; color: #888;">
            You received this email because your business is publicly listed as a WooCommerce user. 
            This is a one-time outreach to share a relevant free tool.
          </p>
          
          <p style="font-size: 12px; color: #888;">
            <a href="{unsubscribe_url}" style="color: #666;">Unsubscribe</a> | 
            <a href="https://routepricing.com" style="color: #666;">RoutePricing.com</a>
          </p>
        </div>
        <img src="{tracking_pixel_url}" width="1" height="1" alt="" style="display:none;">
      </body>
    </html>
    """
    
    # Plain text alternative for better deliverability
    plain_text = f"""
Hi {username},

📍 ADD REAL-TIME DISTANCE-BASED DELIVERY PRICING TO YOUR WOOCOMMERCE STORE

Our free plugin for WooCommerce lets you automatically calculate delivery costs per mile or kilometer, based on the real-time distance between your store and your customer — powered by the Google Maps API.

It's perfect for restaurants, couriers, service businesses, or anyone who wants to set accurate delivery fees that scale with distance.

✅ Easy setup
✅ Real-time calculation at checkout
✅ Fully free on WordPress.org

Get the plugin now: {click_tracking_url}

Let me know if you have any questions about setting up distance-based pricing for your store!

Best regards,
The RoutePricing Team

---

You received this email because your business is publicly listed as a WooCommerce user. This is a one-time outreach to share a relevant free tool.

Unsubscribe: {unsubscribe_url}
RoutePricing.com: https://routepricing.com
    """
    
    return subject, html_content, plain_text

# Validate email address
def is_valid_email(email):
    return bool(EMAIL_REGEX.match(email))

# Send an individual email using SendGrid
def send_email(to_email, tracking_id):
    try:
        # Skip invalid email addresses
        if not is_valid_email(to_email):
            logger.warning(f"Invalid email format: {to_email}")
            return False
        
        # Get application URL from environment or use default
        host_url = os.environ.get('APP_URL', 'http://localhost:5000')
        subject, html_content, plain_text = personalize_email(to_email, tracking_id, host_url=host_url)
        
        # Create SendGrid client
        sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)
        
        # Create from email
        from_email = Email(email=EMAIL_ADDRESS, name=SENDER_NAME)
        
        # Create to email
        to_email_obj = To(email=to_email)
        
        # Create email content
        plain_content = Content("text/plain", plain_text)
        html_content_obj = Content("text/html", html_content)
        
        # Create mail object
        mail = Mail(from_email=from_email, to_emails=to_email_obj, subject=subject)
        
        # Add content to the mail
        mail.content = [plain_content, html_content_obj]
        
        # Add custom headers
        mail.header = {
            "List-Unsubscribe": f"<mailto:unsubscribe@routepricing.com?subject=Unsubscribe>, <{host_url}/unsubscribe?email={to_email}&id={tracking_id}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"
        }
        
        # Add custom tracking settings if needed
        mail.tracking_settings = {
            "click_tracking": {
                "enable": True,
                "enable_text": True
            },
            "open_tracking": {
                "enable": True,
            }
        }
        
        # Send the email
        response = sg.client.mail.send.post(request_body=mail.get())
        
        if response.status_code >= 200 and response.status_code < 300:
            logger.info(f"✅ Email sent to {to_email} (Status: {response.status_code})")
            return True
        else:
            logger.error(f"❌ Failed to send email to {to_email}: SendGrid returned status {response.status_code}")
            return False
        
    except Exception as e:
        logger.error(f"❌ Failed to send email to {to_email}: {str(e)}")
        return False

# Process the CSV file and send emails
def process_emails(file_path, batch_size=DEFAULT_BATCH_SIZE):
    # Load necessary data
    progress = load_email_progress()
    schedule = load_sending_schedule()
    
    # Update the sending schedule based on current date
    schedule = update_sending_schedule(schedule)
    
    sent_emails = load_sent_emails()
    email_history = load_email_history()
    unsubscribed = load_unsubscribed()
    
    # Calculate daily limit based on progressive schedule
    daily_limit = calculate_daily_limit(progress, schedule)
    
    # Log sending schedule information
    current_week = schedule["current_week"]
    weekly_limit = PROGRESSIVE_SCHEDULE.get(current_week, PROGRESSIVE_SCHEDULE[5])
    weekly_sent = schedule["weekly_sent"]
    
    logger.info(f"🚀 Week {current_week}: {weekly_sent}/{weekly_limit} emails sent this week")
    logger.info(f"🚀 Daily email limit: {daily_limit}")
    
    # Extract emails from the CSV file
    email_list = []
    try:
        with open(file_path, newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                # Look for common email field names
                email_field = None
                for field in ['email', 'Email', 'EMAIL', 'email_address', 'Email Address']:
                    if field in row:
                        email_field = field
                        break
                
                if email_field and row[email_field].strip():
                    email_list.append(row[email_field].strip())
    except Exception as e:
        logger.error(f"Error reading CSV file: {str(e)}")
        return {"status": "error", "message": f"Error reading CSV file: {str(e)}"}
    
    if not email_list:
        return {"status": "error", "message": "No valid emails found in the CSV file"}
    
    # Process emails in batches
    processed_count = 0
    skipped_count = 0
    batch_number = 1
    
    # Create a list of valid emails that aren't already sent or unsubscribed
    valid_emails = [email for email in email_list if 
                    email not in sent_emails and 
                    email not in unsubscribed and 
                    is_valid_email(email)]
    
    logger.info(f"Found {len(valid_emails)} valid emails to process")
    
    # Calculate how many batches we'll process today
    emails_to_process = min(daily_limit, len(valid_emails))
    num_batches = (emails_to_process + batch_size - 1) // batch_size  # Ceiling division
    
    logger.info(f"Will process {emails_to_process} emails in {num_batches} batches of up to {batch_size} emails each")
    
    # Process emails in batches
    for batch_start in range(0, emails_to_process, batch_size):
        # Create the current batch
        batch_end = min(batch_start + batch_size, emails_to_process)
        current_batch = valid_emails[batch_start:batch_end]
        batch_size_actual = len(current_batch)
        
        logger.info(f"Processing batch {batch_number}/{num_batches} ({batch_size_actual} emails)")
        
        # Process each email in the batch
        batch_successful = 0
        for email in current_batch:
            # Generate tracking ID for this email
            tracking_id = generate_tracking_id(email)
            
            # Send the email
            if send_email(email, tracking_id):
                # Update tracking data
                email_history[tracking_id] = {
                    "email": email,
                    "sent_at": str(datetime.datetime.now()),
                    "opened": False,
                    "opened_at": None,
                    "clicked": False,
                    "clicked_at": None
                }
                
                # Update progress and counters
                sent_emails.add(email)
                processed_count += 1
                batch_successful += 1
                progress['emails_sent_today'] += 1
                progress['emails_sent_this_month'] += 1
                schedule['weekly_sent'] += 1
                
                # Add random delay between emails
                delay = random.uniform(MIN_DELAY, MAX_DELAY)
                time.sleep(delay)
        
        logger.info(f"Batch {batch_number} complete: {batch_successful}/{batch_size_actual} emails sent successfully")
        
        # Save progress after each batch
        save_email_progress(progress)
        save_sending_schedule(schedule)
        save_sent_emails(sent_emails)
        save_email_history(email_history)
        
        batch_number += 1
        
        # Pause between batches to reduce risk of triggering spam detection
        if batch_number <= num_batches:  # Don't pause after the last batch
            pause_time = BATCH_PAUSE + random.randint(-10, 10)  # Add some randomness
            logger.info(f"Pausing for {pause_time} seconds between batches...")
            time.sleep(pause_time)
    
    # Final save of all data
    save_email_progress(progress)
    save_sending_schedule(schedule)
    save_sent_emails(sent_emails)
    save_email_history(email_history)
    
    # Calculate remaining emails for today
    remaining_today = daily_limit - processed_count
    
    # Get current week's statistics
    current_week = schedule["current_week"]
    weekly_limit = PROGRESSIVE_SCHEDULE.get(current_week, PROGRESSIVE_SCHEDULE[5])
    weekly_sent = schedule["weekly_sent"]
    weekly_remaining = weekly_limit - weekly_sent
    
    return {
        "status": "success", 
        "sent": processed_count,
        "skipped": skipped_count,
        "remaining_today": remaining_today,
        "batches_processed": batch_number - 1,
        "week_number": current_week,
        "weekly_sent": weekly_sent,
        "weekly_limit": weekly_limit,
        "weekly_remaining": weekly_remaining
    }

# Add a new route to handle scheduled processing
@app.route('/process/<path:file_path>', methods=['GET'])
def scheduled_processing(file_path):
    """API endpoint for scheduled processing of emails."""
    try:
        # Validate the file path
        if not os.path.exists(file_path):
            return jsonify({"status": "error", "message": f"File not found: {file_path}"}), 404
        
        # Get batch size from query parameters, default to DEFAULT_BATCH_SIZE
        batch_size = request.args.get('batch_size', default=DEFAULT_BATCH_SIZE, type=int)
        
        # Process the emails
        result = process_emails(file_path, batch_size=batch_size)
        
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error in scheduled processing: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# Flask routes
@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'csv_file' not in request.files:
        return "No file part", 400
    
    file = request.files['csv_file']
    if file.filename == '':
        return "No selected file", 400
    
    if file and file.filename.endswith('.csv'):
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(file.filename))
        file.save(file_path)
        
        # Get batch size from form, default to DEFAULT_BATCH_SIZE if not provided
        batch_size = int(request.form.get('batch_size', DEFAULT_BATCH_SIZE))
        
        # Process the emails
        result = process_emails(file_path, batch_size=batch_size)
        
        if result["status"] == "success":
            return render_template('success.html', 
                                   sent=result["sent"], 
                                   skipped=result["skipped"],
                                   remaining=result["remaining_today"],
                                   batches=result.get("batches_processed", 0),
                                   week_number=result.get("week_number", 1),
                                   weekly_sent=result.get("weekly_sent", 0),
                                   weekly_limit=result.get("weekly_limit", 700),
                                   weekly_remaining=result.get("weekly_remaining", 0))
        else:
            return result["message"], 400
    else:
        return "Invalid file type. Please upload a CSV file.", 400

@app.route('/track/<tracking_id>')
def track_email(tracking_id):
    """Route to handle the email tracking pixel."""
    # Load email history
    email_history = load_email_history()
    
    # Update tracking information if the ID exists
    if tracking_id in email_history:
        if not email_history[tracking_id]["opened"]:
            email_history[tracking_id]["opened"] = True
            email_history[tracking_id]["opened_at"] = str(datetime.datetime.now())
            save_email_history(email_history)
    
    # Return a transparent 1x1 GIF
    transparent_gif = base64.b64decode(
        "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    )
    return Response(transparent_gif, mimetype='image/gif')

@app.route('/click/<tracking_id>')
def track_click(tracking_id):
    """Route to handle link clicks."""
    # Load email history
    email_history = load_email_history()
    
    # Update tracking information if the ID exists
    if tracking_id in email_history:
        email_history[tracking_id]["clicked"] = True
        email_history[tracking_id]["clicked_at"] = str(datetime.datetime.now())
        save_email_history(email_history)
    
    # Redirect to the destination URL
    destination = request.args.get('url', 'https://wordpress.org/plugins/calculate-prices-based-on-distance-for-woocommerce/')
    return redirect(destination)

@app.route('/unsubscribe')
def unsubscribe():
    """Handle unsubscribe requests."""
    email = request.args.get('email')
    tracking_id = request.args.get('id')
    
    if not email:
        return "Email address is required", 400
    
    # Load unsubscribed list
    unsubscribed = load_unsubscribed()
    
    # Add email to unsubscribed list
    unsubscribed.add(email)
    save_unsubscribed(unsubscribed)
    
    # Update tracking data if ID is provided
    if tracking_id:
        email_history = load_email_history()
        if tracking_id in email_history:
            if not email_history[tracking_id].get("unsubscribed", False):
                email_history[tracking_id]["unsubscribed"] = True
                email_history[tracking_id]["unsubscribed_at"] = str(datetime.datetime.now())
                save_email_history(email_history)
    
    return render_template('unsubscribed.html', email=email)

@app.route('/stats')
def stats():
    """Display email statistics."""
    # Load data
    progress = load_email_progress()
    email_history = load_email_history()
    sent_emails = load_sent_emails()
    unsubscribed = load_unsubscribed()
    schedule = load_sending_schedule()
    
    # Update the sending schedule
    schedule = update_sending_schedule(schedule)
    
    # Calculate statistics
    total_sent = len(sent_emails)
    total_opened = sum(1 for record in email_history.values() if record.get("opened", False))
    total_clicked = sum(1 for record in email_history.values() if record.get("clicked", False))
    total_unsubscribed = len(unsubscribed)
    
    open_rate = (total_opened / total_sent * 100) if total_sent > 0 else 0
    click_rate = (total_clicked / total_sent * 100) if total_sent > 0 else 0
    
    # Recent activity (last 50 emails)
    recent_activity = sorted(
        [record for record in email_history.values()],
        key=lambda x: x.get("sent_at", ""),
        reverse=True
    )[:50]
    
    # Calculate remaining emails for today
    daily_limit = calculate_daily_limit(progress, schedule)
    remaining_today = daily_limit
    
    return render_template('stats.html', 
                        total_sent=total_sent,
                        total_opened=total_opened,
                        total_clicked=total_clicked,
                        total_unsubscribed=total_unsubscribed,
                        open_rate=open_rate,
                        click_rate=click_rate,
                        sent_today=progress["emails_sent_today"],
                        sent_this_month=progress["emails_sent_this_month"],
                        remaining_today=remaining_today,
                        daily_limit=MAX_DAILY_EMAILS,
                        monthly_limit=MONTHLY_EMAIL_CAP,
                        recent_activity=recent_activity)


@app.route('/batch')
def batch_form():
    """Display a form for configuring batch processing."""
    # Load progress to show current status
    progress = load_email_progress()
    schedule = load_sending_schedule()
    daily_limit = calculate_daily_limit(progress, schedule)
    
    # Get a list of available CSV files
    csv_files = []
    for root, dirs, files in os.walk(app.config['UPLOAD_FOLDER']):
        for file in files:
            if file.endswith('.csv'):
                file_path = os.path.join(root, file)
                file_size = os.path.getsize(file_path) / 1024  # Size in KB
                csv_files.append({
                    "path": file_path, 
                    "name": file, 
                    "size": f"{file_size:.2f} KB"
                })
    
    return render_template('batch.html',
                        csv_files=csv_files,
                        sent_today=progress.get("emails_sent_today", 0),
                        remaining_today=daily_limit,
                        default_batch_size=DEFAULT_BATCH_SIZE)


# Helper function for secure filenames
def secure_filename(filename):
    """Sanitize filename to prevent directory traversal attacks."""
    return os.path.basename(filename)


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
        
    """cmd export SENDGRID_API_KEY='your_sendgrid_api_key'"""     
                    