"""
email_handler.py - Email notification system for PantryAI
--------------------------------------------------------
Handles sending reorder confirmation emails and monitoring replies
"""

import smtplib
import imaplib
import email
import time
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import os
import logging

logger = logging.getLogger(__name__)

# Email configuration - UPDATE THESE WITH YOUR DETAILS
EMAIL_CONFIG = {
    'smtp_server': 'smtp.gmail.com',
    'smtp_port': 587,
    'sender_email': 'your_email@gmail.com',  # Your Gmail address
    'sender_password': 'your_app_password',   # Gmail App Password (16 chars)
    'recipient_email': 'your_email@gmail.com'  # Where to send notifications
}

# Product to Blinkit search mapping
PRODUCT_SEARCH_MAP = {
    'Rice Container': 'basmati rice 1kg',
    'Pulse Container': 'toor dal 1kg',
    'Grain Container': 'wheat flour 1kg',
    'Water Bottle': 'water bottle 1L pack'
}

def send_reorder_confirmation_email(product_name, current_percent):
    """
    Send reorder confirmation email to user
    Args:
        product_name: Name of the product that needs reordering
        current_percent: Current fill percentage
    """
    try:
        # Create email message
        msg = MIMEMultipart()
        msg['From'] = EMAIL_CONFIG['sender_email']
        msg['To'] = EMAIL_CONFIG['recipient_email']
        msg['Subject'] = f'PantryAI: {product_name} Running Low - Reorder Confirmation'
        
        # Email body
        search_term = PRODUCT_SEARCH_MAP.get(product_name, product_name.lower())
        
        body = f"""
Hello!

PantryAI has detected that your {product_name} is running low.

Current Level: {current_percent}%
Threshold: 30%

Product will be searched on Blinkit as: "{search_term}"

Would you like PantryAI to automatically add this item to your Blinkit cart?

Please reply with:
- "YES" to proceed with automatic ordering
- "NO" to skip this reorder

If you don't reply within 5 minutes, the reorder will be skipped automatically.

Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Best regards,
PantryAI System
        """
        
        msg.attach(MIMEText(body, 'plain'))
        
        # Send email
        with smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port']) as server:
            server.starttls()
            server.login(EMAIL_CONFIG['sender_email'], EMAIL_CONFIG['sender_password'])
            server.send_message(msg)
        
        logger.info(f"Reorder confirmation email sent for {product_name}")
        
        # Start monitoring for reply in separate thread
        reply_thread = threading.Thread(
            target=monitor_email_reply, 
            args=(product_name, search_term),
            daemon=True
        )
        reply_thread.start()
        
    except Exception as e:
        logger.error(f"Failed to send reorder email: {e}")
        # Fallback - proceed with reorder anyway
        logger.info("Proceeding with automatic reorder due to email failure")
        from blinkit_handler import start_blinkit_reorder
        start_blinkit_reorder(product_name, search_term)

def monitor_email_reply(product_name, search_term, timeout_minutes=5):
    """
    Monitor email for user reply (YES/NO)
    Args:
        product_name: Product that needs reordering
        search_term: Blinkit search term
        timeout_minutes: How long to wait for reply
    """
    start_time = time.time()
    timeout_seconds = timeout_minutes * 60
    
    logger.info(f"Monitoring email reply for {product_name}...")
    
    while time.time() - start_time < timeout_seconds:
        try:
            # Connect to email
            mail = imaplib.IMAP4_SSL('imap.gmail.com')
            mail.login(EMAIL_CONFIG['sender_email'], EMAIL_CONFIG['sender_password'])
            mail.select('inbox')
            
            # Search for recent unread emails
            search_criteria = f'(UNSEEN SUBJECT "PantryAI: {product_name}")'
            status, messages = mail.search(None, search_criteria)
            
            if messages[0]:
                # Get the latest message
                msg_ids = messages[0].split()
                if msg_ids:
                    latest_msg_id = msg_ids[-1]
                    status, msg_data = mail.fetch(latest_msg_id, '(RFC822)')
                    
                    # Parse email
                    raw_email = msg_data[0][1]
                    email_message = email.message_from_bytes(raw_email)
                    
                    # Get email body
                    body = ""
                    if email_message.is_multipart():
                        for part in email_message.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode()
                                break
                    else:
                        body = email_message.get_payload(decode=True).decode()
                    
                    # Check reply
                    body_upper = body.upper().strip()
                    
                    if 'YES' in body_upper:
                        logger.info(f"User confirmed reorder for {product_name}")
                        # Mark email as read
                        mail.store(latest_msg_id, '+FLAGS', '\\Seen')
                        mail.logout()
                        
                        # Proceed with Blinkit reorder
                        from blinkit_handler import start_blinkit_reorder
                        start_blinkit_reorder(product_name, search_term)
                        return
                        
                    elif 'NO' in body_upper:
                        logger.info(f"User declined reorder for {product_name}")
                        # Mark email as read
                        mail.store(latest_msg_id, '+FLAGS', '\\Seen')
                        mail.logout()
                        return
            
            mail.logout()
            time.sleep(15)  # Check every 15 seconds
            
        except Exception as e:
            logger.error(f"Error checking email reply: {e}")
            time.sleep(30)  # Wait longer if there's an error
    
    # Timeout reached
    logger.info(f"No reply received for {product_name} within {timeout_minutes} minutes - skipping reorder")

def send_reorder_status_email(product_name, status, message=""):
    """
    Send status update about reorder process
    Args:
        product_name: Product name
        status: 'success', 'failed', 'manual_needed'
        message: Additional message
    """
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_CONFIG['sender_email']
        msg['To'] = EMAIL_CONFIG['recipient_email']
        
        if status == 'success':
            msg['Subject'] = f'PantryAI: {product_name} Added to Blinkit Cart'
            body = f"""
{product_name} has been successfully added to your Blinkit cart!

Please open Blinkit to complete the checkout and payment.

{message}

Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            """
        elif status == 'failed':
            msg['Subject'] = f'PantryAI: Failed to Add {product_name} to Cart'
            body = f"""
PantryAI was unable to automatically add {product_name} to your Blinkit cart.

Reason: {message}

Please manually order this item on Blinkit.
Search term: {PRODUCT_SEARCH_MAP.get(product_name, product_name)}

Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            """
        else:  # manual_needed
            msg['Subject'] = f'PantryAI: Manual Action Required for {product_name}'
            body = f"""
{product_name} has been added to your Blinkit cart but requires manual action.

{message}

Please open Blinkit to complete the process.

Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            """
        
        msg.attach(MIMEText(body, 'plain'))
        
        with smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port']) as server:
            server.starttls()
            server.login(EMAIL_CONFIG['sender_email'], EMAIL_CONFIG['sender_password'])
            server.send_message(msg)
        
        logger.info(f"Status email sent for {product_name}: {status}")
        
    except Exception as e:
        logger.error(f"Failed to send status email: {e}")

# Email configuration validation
def validate_email_config():
    """Validate email configuration"""
    if EMAIL_CONFIG['sender_email'] == 'your_email@gmail.com':
        logger.error("Please update EMAIL_CONFIG with your actual Gmail credentials in email_handler.py")
        return False
    
    if EMAIL_CONFIG['sender_password'] == 'your_app_password':
        logger.error("Please set your Gmail App Password in email_handler.py")
        return False
        
    return True

if __name__ == '__main__':
    # Test email functionality
    if validate_email_config():
        print("Testing email functionality...")
        send_reorder_confirmation_email("Test Product", 25)
        print("Check your email for test message")
    else:
        print("Please configure email settings first")