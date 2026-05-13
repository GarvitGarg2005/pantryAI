# PantryAI – Smart Pantry Monitoring and Automated Reordering System

## Overview

PantryAI is a computer vision based pantry monitoring system designed to track household inventory levels in real time and automate the reordering process when stock becomes low.

The system uses a live camera feed along with YOLO object detection, OpenCV based image analysis, inventory management logic, a live dashboard, and automated Blinkit integration for reordering.

The project is divided into multiple modules, each handling a different part of the workflow.

---

# Overall Flow of the System

The complete flow of the project works as follows:

1. The camera continuously captures live video frames.
2. YOLO detects containers/items visible in front of the camera.
3. OpenCV analyzes the detected containers to estimate how full they are.
4. Inventory levels are updated continuously.
5. If a product level drops below a threshold:
   - the item is marked as low stock
   - a reorder request is generated
6. An email is sent to the user asking for confirmation.
7. If the user replies YES:
   - Blinkit opens automatically
   - location permission is handled automatically
   - the product is searched
   - the first matching item is added to cart

The frontend dashboard simultaneously displays:
- live camera feed
- current inventory status
- low stock alerts
- reorder logs
- item percentages

---

# File Structure and Functionality

## 1. detector.py – Vision Pipeline

This file handles the complete computer vision pipeline of the project. :contentReference[oaicite:0]{index=0}

### Main Responsibilities

- Uses YOLOv8 for object detection
- Detects pantry related containers such as:
  - bowls
  - bottles
  - cups
  - jars
- Runs OpenCV based image analysis inside detected container regions
- Classifies contents like:
  - Rice
  - Dal
  - Water bottle
  - Biscuit packet
- Estimates approximate fill level percentages

### How Detection Works

The logic works in two stages:

### Stage 1 — Container Detection

YOLO first detects only the outer container/object.

For example:
- bowl
- bottle
- cup
- vase

### Stage 2 — Content Analysis

After detecting the container, OpenCV analyzes the inside region of the bounding box using HSV colour analysis.

Examples:
- white/cream coloured pixels → Rice container
- yellow/orange coloured pixels → Dal container

The percentage of detected colour inside the container is then used to estimate how full the container is.

This avoids detecting unrelated objects in the background and improves accuracy for pantry items. :contentReference[oaicite:1]{index=1}

### Additional Features

- Fill level estimation
- Absence detection
- Real time bounding boxes
- Inventory updates
- Live frame annotations

---

# 2. inventory.py – Inventory Management Logic

This file manages the internal inventory state of the pantry system. :contentReference[oaicite:2]{index=2}

### Main Responsibilities

- Stores current inventory levels
- Tracks stock percentages
- Decides when an item becomes low stock
- Handles reorder queue generation
- Prevents duplicate reorder emails
- Detects restocking events

### Reorder Logic

Each item has:
- a threshold percentage
- a cooldown timer
- a reorder status
- a reorder queue state

If an item's quantity falls below the threshold:
- it gets added to the reorder queue
- reorder email logic is triggered

When the item is restocked:
- the system re-arms the item
- clears cooldown
- allows future reorder triggers again

This makes the system behave more realistically and avoids repeated reorder spam. :contentReference[oaicite:3]{index=3}

---

# 3. app.py – Main Backend Server

This file acts as the central controller of the entire system.

### Main Responsibilities

- Starts Flask backend server
- Starts camera thread
- Starts AI inference thread
- Connects detector with inventory manager
- Streams live camera feed
- Sends live updates to frontend
- Exposes APIs for dashboard and reorder actions

### Threads Used

The project uses multithreading to improve smoothness and performance.

Separate threads are used for:
- camera capture
- YOLO inference
- Flask server
- reorder engine

This prevents the UI and video feed from freezing while detection is running.

### APIs Provided

The backend exposes APIs for:
- live video stream
- inventory snapshots
- reorder actions
- real time event updates

---

# 4. reorder.py – Automated Reordering Engine

This file contains the automation logic for reordering products. :contentReference[oaicite:4]{index=4}

### Main Responsibilities

- Sends reorder confirmation emails
- Waits for YES/NO reply from Gmail
- Opens Blinkit automatically
- Handles location permission popup
- Searches for products
- Adds products to Blinkit cart automatically

### Email Workflow

When stock becomes low:
1. An email is sent to the user
2. The system waits for a YES/NO reply
3. Blinkit is NOT opened until approval is received

This prevents accidental reorders.

The email system uses:
- SMTP for sending mail
- IMAP for reading replies from Gmail

### Blinkit Automation

After receiving YES:
- Selenium opens Blinkit
- location popup is handled automatically
- search bar is activated
- product is searched
- first result is added to cart

This entire process is automated through Selenium browser automation. :contentReference[oaicite:5]{index=5}

---

# 5. index.html – Frontend Dashboard

This file contains the frontend dashboard UI of PantryAI. :contentReference[oaicite:6]{index=6}

### Main Features

- Live camera stream
- Real time inventory display
- Fill percentage bars
- Low stock indicators
- Reorder logs
- Manual reorder button
- Real time alerts/toasts

### Frontend Technologies Used

- HTML
- CSS
- JavaScript
- Server Sent Events (SSE)

### Live Updating

The frontend continuously receives live updates from Flask using SSE without refreshing the page.

This allows:
- instant stock updates
- real time notifications
- live reorder status changes

---

# Technologies Used

- Python
- Flask
- OpenCV
- YOLOv8
- Ultralytics
- NumPy
- Selenium
- SQLite
- HTML/CSS/JavaScript
- Gmail SMTP/IMAP
- Blinkit Automation

---

# Key Learning Outcomes

Through this project I learned:
- real time computer vision pipelines
- object detection using YOLO
- image processing using OpenCV
- multithreaded backend systems
- Flask API development
- frontend-backend integration
- Selenium browser automation
- real time event streaming
- inventory management logic
- automated workflow systems

---

# Future Improvements

Some possible future improvements include:

- training a custom pantry dataset
- improving fill level estimation accuracy
- adding object tracking
- mobile app integration
- voice assistant support
- barcode scanning
- better product matching on Blinkit
- cloud database integration
- user authentication

---

# Conclusion

PantryAI combines computer vision, automation, and inventory management into a single real time smart pantry system.

The project demonstrates how AI based monitoring can be integrated with practical automation tools to create a system capable of:
- detecting pantry items
- estimating stock levels
- monitoring inventory
- notifying users
- automating the reordering workflow

The main focus of the project was to build a realistic end-to-end pipeline connecting AI detection with real world automation.
