"""
reorder.py  –  PantryAI Reorder Engine
----------------------------------------
Flow per item:
  1. Send confirmation email with a UNIQUE subject key (item + timestamp).
  2. Poll Gmail ONLY for that exact subject key.
     Blinkit browser is NOT opened until YES arrives.
  3. YES  → open Blinkit, handle location, search, add first result to cart.
  4. NO / TIMEOUT → mark skipped.

Key design decisions
---------------------
- _active tracks items that have an email OUT waiting for a reply.
  It is cleared RIGHT AFTER the reply arrives (before Blinkit opens),
  so a new restock → low cycle can immediately send a fresh email even
  while a previous Blinkit session is still open for the same item.
- We never auto-approve. If email is not configured we log and abort.
- Each item's thread is fully independent; multiple items work concurrently.

.env file (project root):
  GMAIL_SENDER=you@gmail.com
  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
  NOTIFY_EMAIL=you@gmail.com
"""

import os
import time
import threading
import logging
import smtplib
import imaplib
import email as email_lib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, ElementNotInteractableException
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="[REORDER] %(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SENDER   = os.getenv("GMAIL_SENDER",       "").strip()
GMAIL_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
NOTIFY_EMAIL   = os.getenv("NOTIFY_EMAIL", GMAIL_SENDER).strip()

REPLY_TIMEOUT      = 300    # seconds to wait for YES/NO (5 min)
LOCATION_WAIT_SECS = 12     # seconds to let Blinkit resolve location
BLINKIT_URL        = "https://blinkit.com"


# ── Env validation ────────────────────────────────────────────────────────────

def _check_env():
    missing = []
    if not GMAIL_SENDER:
        missing.append("GMAIL_SENDER")
    if not GMAIL_PASSWORD:
        missing.append("GMAIL_APP_PASSWORD")
    if missing:
        raise EnvironmentError(
            f"Missing .env variables: {', '.join(missing)}\n"
            "Create a .env file with:\n"
            "  GMAIL_SENDER=you@gmail.com\n"
            "  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx\n"
            "  NOTIFY_EMAIL=you@gmail.com\n"
            "Get App Password: Google Account → Security → "
            "2-Step Verification → App passwords"
        )


# ── Email helpers ─────────────────────────────────────────────────────────────

class EmailNotifier:

    def send(self, subject: str, body: str):
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_SENDER
        msg["To"]      = NOTIFY_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(GMAIL_SENDER, GMAIL_PASSWORD)
            srv.send_message(msg)
        log.info(f"📤 Email sent → {NOTIFY_EMAIL} | Subject: {subject}")

    def wait_for_reply(self, subject_keyword: str,
                       timeout: int = REPLY_TIMEOUT) -> str:
        """
        Poll Gmail every 15 s for an UNREAD reply whose subject contains
        subject_keyword (unique per reorder: item + unix timestamp).

        Returns 'YES', 'NO', or 'TIMEOUT'.
        Blinkit is NOT opened until this returns 'YES'.

        Safety: subject_keyword contains a unix timestamp so an old email
        from a prior reorder cycle will never match.
        """
        deadline = time.time() + timeout
        log.info(
            f"⏳ Waiting up to {timeout // 60}m {timeout % 60}s for reply "
            f"to [{subject_keyword}] — Blinkit stays closed until YES."
        )

        while time.time() < deadline:
            time.sleep(15)
            mail = None
            try:
                mail = imaplib.IMAP4_SSL("imap.gmail.com")
                mail.login(GMAIL_SENDER, GMAIL_PASSWORD)
                mail.select("inbox")

                _, data = mail.search(
                    None, f'(UNSEEN SUBJECT "{subject_keyword}")'
                )
                ids = data[0].split()

                if not ids:
                    mail.logout()
                    mail = None
                    continue

                # Read the latest matching message
                _, msg_data = mail.fetch(ids[-1], "(RFC822)")
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(
                                decode=True).decode(errors="replace")
                            break
                else:
                    body = msg.get_payload(
                        decode=True).decode(errors="replace")

                # Mark as read immediately so we don't re-process
                mail.store(ids[-1], "+FLAGS", "\\Seen")
                mail.logout()
                mail = None

                # Check the FIRST non-empty line (ignore quoted reply history)
                first_line = next(
                    (l.strip().upper()
                     for l in body.splitlines() if l.strip()), ""
                )
                full_upper = body.strip().upper()

                if first_line.startswith("YES") or full_upper.startswith("YES"):
                    log.info("✅ Reply: YES — will now open Blinkit.")
                    return "YES"
                if first_line.startswith("NO") or full_upper.startswith("NO"):
                    log.info("❌ Reply: NO")
                    return "NO"

                log.warning(
                    f"Reply received but first line was {first_line!r}. "
                    "Waiting for a clearer YES or NO …"
                )

            except Exception as exc:
                log.warning(f"Email poll error: {exc}")
            finally:
                if mail:
                    try:
                        mail.logout()
                    except Exception:
                        pass

        log.warning(f"⏰ Timeout — no reply for [{subject_keyword}]")
        return "TIMEOUT"


# ── Blinkit automation ────────────────────────────────────────────────────────

def _build_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--window-size=1366,768")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option(
        "prefs",
        {"profile.default_content_setting_values.geolocation": 1}
    )
    service = Service(ChromeDriverManager().install())
    driver  = webdriver.Chrome(service=service, options=opts)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


class BlinkitSession:

    def __init__(self):
        self.driver = _build_driver()
        self.wait   = WebDriverWait(self.driver, 20)

    def open_and_set_location(self):
        log.info("🌐 Opening Blinkit …")
        self.driver.get(BLINKIT_URL)
        time.sleep(4)
        self._dismiss_location_modal()
        log.info(f"📍 Waiting {LOCATION_WAIT_SECS}s for location to resolve …")
        time.sleep(LOCATION_WAIT_SECS)
        log.info("📍 Location resolved.")

    def search_and_add(self, query: str) -> bool:
        """
        Search for query and add the first result to cart.

        Blinkit's product cards are React components where:
          - The entire card is wrapped in an <a> tag → clicks navigate/zoom
          - The ADD button is a <div> that only becomes visible on hover
          - The "+" sits in the bottom-right corner of the card

        Strategy:
          1. Wait for product cards to load
          2. Find the FIRST product card container
          3. Hover over it so Blinkit reveals the ADD button
          4. Re-find the ADD button (now visible) and click it via JS
             (JS click bypasses the <a> tag interception)
          5. Fall back to elementFromPoint click at the card's bottom-right
        """
        from selenium.webdriver.common.action_chains import ActionChains

        log.info(f'🔍 Searching: "{query}"')
        if not self._activate_and_type(query):
            return False

        # Wait for product cards to appear
        try:
            WebDriverWait(self.driver, 10).until(
                lambda d: len(d.find_elements(By.XPATH,
                    '//*[contains(@class,"Product") or '
                    'contains(@class,"product") or '
                    'contains(@class,"item") or '
                    'contains(@class,"Item")]')) > 0
            )
            log.info("Product cards detected.")
        except TimeoutException:
            log.warning("Product cards timeout — continuing anyway.")
        time.sleep(1.5)

        # ── Step 1: hover over the first product card to reveal ADD button ──
        card = self._find_first_product_card()
        if card:
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", card
                )
                time.sleep(0.5)
                ActionChains(self.driver).move_to_element(card).perform()
                log.info("Hovered over first product card.")
                time.sleep(0.8)   # give React time to render the ADD button
            except Exception as exc:
                log.warning(f"Hover failed: {exc}")

        # ── Step 2: find the ADD button (now visible after hover) ───────────
        btn = self._find_add_button()

        if btn:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", btn
            )
            time.sleep(0.5)

            # Try JS click first — bypasses the <a> overlay completely
            try:
                self.driver.execute_script("arguments[0].click();", btn)
                log.info("✅ Clicked ADD button via JS click.")
                time.sleep(2)
                return True
            except Exception as exc:
                log.warning(f"JS click failed: {exc}")

            # Try ActionChains direct click
            try:
                ActionChains(self.driver).move_to_element(btn).click().perform()
                log.info("✅ Clicked ADD button via ActionChains.")
                time.sleep(2)
                return True
            except Exception as exc:
                log.warning(f"ActionChains click failed: {exc}")

        # ── Step 3: elementFromPoint — click at the pixel where ADD sits ────
        # Blinkit's ADD "+" is always at the BOTTOM-RIGHT of the first card.
        # We use JS to find the topmost element at that coordinate and click it.
        log.warning("Element-based clicks failed — trying elementFromPoint.")
        if card:
            try:
                success = self.driver.execute_script("""
                    var card = arguments[0];
                    var rect = card.getBoundingClientRect();
                    // Bottom-right corner where the ADD button lives
                    var x = rect.right  - 20;
                    var y = rect.bottom - 20;
                    var el = document.elementFromPoint(x, y);
                    if (el) {
                        el.click();
                        return el.tagName + '|' + el.className;
                    }
                    return null;
                """, card)
                if success:
                    log.info(f"✅ elementFromPoint click: {success}")
                    time.sleep(2)
                    return True
                else:
                    log.error("elementFromPoint returned null.")
            except Exception as exc:
                log.error(f"elementFromPoint failed: {exc}")

        # ── Step 4: last resort — dump page and give up ──────────────────────
        log.error("All click strategies failed.")
        self._dump_page_info()
        return False

    def quit(self):
        try:
            self.driver.quit()
        except Exception:
            pass

    # ── Internal ────────────────────────────────────────────────────────────

    def _dismiss_location_modal(self):
        xpaths = [
            '//button[contains(translate(text(),"abcdefghijklmnopqrstuvwxyz",'
            '"ABCDEFGHIJKLMNOPQRSTUVWXYZ"),"USE MY LOCATION")]',
            '//button[contains(text(),"Detect my location")]',
            '//button[contains(text(),"detect")]',
            '//button[contains(text(),"Allow")]',
        ]
        css = [
            '[data-testid="location-allow-btn"]',
            'button[class*="location"]',
            'button[class*="Location"]',
        ]
        for xpath in xpaths:
            try:
                btn = WebDriverWait(self.driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                btn.click()
                log.info("Clicked location modal (XPath).")
                return
            except TimeoutException:
                continue
        for sel in css:
            try:
                btn = WebDriverWait(self.driver, 2).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                btn.click()
                log.info("Clicked location modal (CSS).")
                return
            except TimeoutException:
                continue
        log.info("No location modal — auto-detected likely.")

    def _activate_and_type(self, query: str) -> bool:
        search_trigger_selectors = [
            '[class*="SearchBar"]', '[class*="searchBar"]',
            '[class*="search-bar"]', '[class*="SearchBox"]',
            '[class*="searchBox"]', '[class*="Search__container"]',
            '[placeholder*="Search"]', '[placeholder*="search"]',
            'header [class*="search"]', 'nav [class*="search"]',
        ]
        for sel in search_trigger_selectors:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    el.click()
                    log.info(f"Clicked search trigger: {sel!r}")
                    time.sleep(1)
                    break
            except Exception:
                continue

        input_el = self._find_any_search_input()
        if input_el is None:
            log.error("No search input found.")
            return False

        try:
            self.driver.execute_script("arguments[0].value = '';", input_el)
            time.sleep(0.2)
            self.driver.execute_script("""
                var el  = arguments[0], val = arguments[1];
                var setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input',  { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            """, input_el, query)
            time.sleep(0.5)
            current_val = input_el.get_attribute("value") or ""
            if query.lower() not in current_val.lower():
                log.info("JS setter didn't stick — send_keys fallback.")
                input_el.click()
                input_el.send_keys(Keys.CONTROL + "a")
                input_el.send_keys(Keys.DELETE)
                time.sleep(0.3)
                input_el.send_keys(query)
                time.sleep(0.5)
        except Exception as exc:
            log.warning(f"JS type failed ({exc}) — send_keys.")
            try:
                input_el.click()
                input_el.send_keys(Keys.CONTROL + "a")
                input_el.send_keys(query)
            except Exception as exc2:
                log.error(f"send_keys also failed: {exc2}")
                return False

        try:
            input_el.send_keys(Keys.RETURN)
        except Exception:
            self.driver.execute_script(
                "arguments[0].dispatchEvent(new KeyboardEvent('keydown',"
                "{key:'Enter',keyCode:13,bubbles:true}))", input_el
            )
        log.info("Search submitted.")
        return True

    def _find_any_search_input(self):
        selectors = [
            'input[placeholder*="Search"]', 'input[placeholder*="search"]',
            'input[type="search"]',
            '[class*="SearchBar"] input', '[class*="searchBar"] input',
            '[class*="SearchInput"] input', '[class*="searchInput"] input',
            'input[data-testid*="search"]', 'input[name="search"]',
            'header input[type="text"]', 'nav input[type="text"]',
            'input[type="text"]',
        ]
        for sel in selectors:
            try:
                for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed() and el.is_enabled():
                        return el
            except Exception:
                continue
        return None

    def _find_first_product_card(self):
        """
        Return the first visible product card container element.
        Used to hover over it so Blinkit reveals the ADD button.
        """
        card_selectors = [
            # Blinkit 2024-2025 known class fragments
            '[class*="Product__UpdatedPlpProductContainer"]',
            '[class*="plp-product"]',
            '[class*="ProductCard"]',
            '[class*="product-card"]',
            '[class*="ProductItem"]',
            '[class*="product_item"]',
            '[class*="ItemCard"]',
            '[class*="item-card"]',
            # Generic: any <a> that wraps a product image
            'a[href*="/products/"]',
            'a[href*="/prd/"]',
        ]
        for sel in card_selectors:
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    if el.is_displayed():
                        log.info(f"Product card found: {sel!r}")
                        return el
            except Exception:
                continue
        return None

    def _find_add_button(self):
        """
        Find Blinkit's ADD-to-cart button.

        Blinkit renders the ADD button as a <div> (not a <button>) inside
        each product card.  It is only visible after hovering the card.
        We search for it after the hover has been performed.
        """
        # Stage 1: look for the green ADD div / button by text
        # Blinkit uses innerText "ADD" (all caps) in their React components
        xpaths = [
            # div with direct text ADD
            '//div[normalize-space(text())="ADD"]',
            '//div[normalize-space(text())="Add"]',
            # button with direct text
            '//button[normalize-space(text())="ADD"]',
            '//button[normalize-space(text())="Add"]',
            # span inside anything
            '//*[normalize-space(text())="ADD" and '
            '(self::button or self::div or self::span)]',
            # "+" icon button patterns
            '//button[contains(@aria-label,"add") or '
            'contains(@aria-label,"Add") or '
            'contains(@aria-label,"ADD")]',
            '//div[contains(@aria-label,"add") or '
            'contains(@aria-label,"Add") or '
            'contains(@aria-label,"ADD")]',
        ]
        for xpath in xpaths:
            try:
                for el in self.driver.find_elements(By.XPATH, xpath):
                    if el.is_displayed():
                        log.info(f"ADD button via XPath: {xpath!r}")
                        return el
            except Exception:
                continue

        # Stage 2: CSS class fragment patterns
        css_sels = [
            # Blinkit-specific observed patterns
            '[class*="UpdatedProductCard__AddBtn"]',
            '[class*="AddToCart"]',
            '[class*="add-to-cart"]',
            '[class*="AddButton"]',
            '[class*="addButton"]',
            '[class*="add_button"]',
            '[class*="plp-button"]',
            '[class*="PlpButton"]',
            '[class*="btn-add"]',
            '[class*="atc"]',
            # data-testid
            '[data-testid*="add"]',
            '[data-testid*="Add"]',
        ]
        for sel in css_sels:
            try:
                for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed():
                        log.info(f"ADD button via CSS: {sel!r}")
                        return el
            except Exception:
                continue

        # Stage 3: scan ALL divs and buttons for "ADD" text
        log.warning("Selector search failed — scanning all elements for ADD text.")
        try:
            for tag in ("button", "div", "span", "a"):
                for el in self.driver.find_elements(By.TAG_NAME, tag):
                    try:
                        if not el.is_displayed():
                            continue
                        txt = (el.text or "").strip()
                        if txt.upper() == "ADD":
                            log.info(f"ADD button via tag scan <{tag}>: {txt!r}")
                            return el
                    except Exception:
                        continue
        except Exception as exc:
            log.error(f"Tag scan failed: {exc}")

        return None

    def _dump_page_info(self):
        """
        Log page URL, all buttons, AND any div/span whose text is 'ADD'.
        This tells you exactly what element Blinkit is rendering for the
        add-to-cart action so you can add a targeted selector.
        """
        try:
            log.error(
                f"=== PAGE DUMP | URL: {self.driver.current_url} "
                f"| Title: {self.driver.title} ==="
            )
            # Dump all buttons
            buttons = self.driver.find_elements(By.TAG_NAME, "button")
            log.error(f"  <button> elements on page: {len(buttons)}")
            for i, btn in enumerate(buttons[:20]):
                try:
                    log.error(
                        f"  btn[{i:02d}] visible={btn.is_displayed()} "
                        f"text={btn.text[:40]!r:42s} "
                        f"class={str(btn.get_attribute('class'))[:70]!r}"
                    )
                except Exception:
                    pass

            # Dump any div/span that has "ADD" as text — the actual Blinkit button
            log.error("  --- div/span with text containing 'ADD' ---")
            for tag in ("div", "span", "a"):
                els = self.driver.find_elements(By.TAG_NAME, tag)
                for el in els:
                    try:
                        txt = (el.text or "").strip()
                        if txt.upper() == "ADD" and el.is_displayed():
                            rect = el.rect
                            log.error(
                                f"  <{tag}> text={txt!r} "
                                f"class={str(el.get_attribute('class'))[:70]!r} "
                                f"rect={rect}"
                            )
                    except Exception:
                        continue
        except Exception as exc:
            log.error(f"Page dump failed: {exc}")


# ── Reorder Engine ────────────────────────────────────────────────────────────

class ReorderEngine:
    """
    Dispatcher pops items from the reorder queue and spawns one thread per
    item.

    _active lifecycle (the critical fix):
      - Item added to _active when its thread starts (email about to be sent).
      - Item REMOVED from _active RIGHT AFTER the email reply is received,
        BEFORE Blinkit opens.
      - This means: while the user is deciding (email waiting) duplicates are
        blocked. But once they reply YES/NO, _active is clear so a new
        restock → low cycle on the same item will immediately send a new email.
    """

    def __init__(self, inventory_manager):
        self.inv      = inventory_manager
        self._running = False
        self._thread  = None
        self._active  = set()
        self._lock    = threading.Lock()

        try:
            _check_env()
            self.notifier = EmailNotifier()
            log.info(f"✅ Email notifier ready. Alerts → {NOTIFY_EMAIL}")
        except EnvironmentError as exc:
            log.error(f"\n{'='*60}\n{exc}\n{'='*60}\n")
            self.notifier = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._dispatcher, daemon=True, name="reorder-dispatcher"
        )
        self._thread.start()
        log.info("ReorderEngine started.")

    def stop(self):
        self._running = False

    def _dispatcher(self):
        while self._running:
            name = self.inv.pop_reorder_queue()
            if name:
                with self._lock:
                    if name in self._active:
                        log.info(
                            f"'{name}' waiting for email reply — "
                            "skipping duplicate queue entry."
                        )
                    else:
                        self._active.add(name)
                        threading.Thread(
                            target=self._run_item,
                            args=(name,),
                            daemon=True,
                            name=f"reorder-{name}",
                        ).start()
            time.sleep(2)

    def _run_item(self, name: str):
        try:
            self._handle(name)
        except Exception as exc:
            log.error(f"Unhandled error for '{name}': {exc}", exc_info=True)
            # Ensure _active is cleared even on unexpected exception
            with self._lock:
                self._active.discard(name)

    def _handle(self, name: str):
        container = self.inv.get_container(name)
        if container is None:
            log.warning(f"Container '{name}' not found.")
            with self._lock:
                self._active.discard(name)
            return

        search_query = container.blinkit_search or name.lower()
        level_pct    = container.level_pct
        # Unique per event: item name + current unix timestamp
        subject_key  = f"PantryAI-{name.replace(' ', '_')}-{int(time.time())}"

        # ── 1. Send email ─────────────────────────────────────────────────
        if self.notifier is None:
            log.error(f"Email not configured — cannot reorder '{name}'.")
            with self._lock:
                self._active.discard(name)
            return

        body = (
            f"PantryAI detected that '{name}' is running low.\n\n"
            f"  Fill level : {level_pct}%\n"
            f"  Blinkit    : \"{search_query}\"\n\n"
            f"Reply YES to open Blinkit and add it to your cart.\n"
            f"Reply NO  to skip this reorder.\n\n"
            f"Blinkit will NOT open until you reply YES.\n"
            f"(Auto-skips in {REPLY_TIMEOUT // 60} minutes if no reply.)"
        )
        try:
            self.notifier.send(
                subject=f"[PantryAI] {subject_key} — Reorder Confirmation",
                body=body,
            )
            self.inv.mark_reorder_sent(name)
        except Exception as exc:
            log.error(f"Failed to send email for '{name}': {exc}")
            with self._lock:
                self._active.discard(name)
            return

        # ── 2. Wait for reply (Blinkit stays CLOSED) ──────────────────────
        log.info(f"🔒 Blinkit CLOSED. Waiting for reply for '{name}' …")
        reply = self.notifier.wait_for_reply(subject_key)

        # ── Clear _active NOW, before opening Blinkit ─────────────────────
        # A new restock → low event can now trigger a fresh email even while
        # we are still in the Blinkit session below.
        with self._lock:
            self._active.discard(name)
        log.info(f"'{name}' cleared from active set (reply={reply!r}).")

        if reply != "YES":
            log.info(f"Skipping reorder for '{name}'.")
            self.inv.mark_reorder_skipped(name)
            return

        # ── 3. Open Blinkit ONLY after YES ────────────────────────────────
        log.info(f"🟢 Opening Blinkit for '{name}' …")
        session = BlinkitSession()
        try:
            session.open_and_set_location()
            success = session.search_and_add(search_query)

            if success:
                self.inv.mark_reorder_done(name)
                self.notifier.send(
                    subject=f"[PantryAI] '{name}' added to Blinkit cart ✓",
                    body=(
                        f"'{name}' has been added to your Blinkit cart.\n"
                        f"Open Blinkit to complete checkout."
                    ),
                )
            else:
                self.notifier.send(
                    subject=f"[PantryAI] Manual action needed — {name}",
                    body=(
                        f"PantryAI could not auto-add '{name}' to Blinkit.\n\n"
                        f"Please order manually:\n"
                        f"  {BLINKIT_URL}\n"
                        f"  Search for: {search_query}"
                    ),
                )

            log.info("Keeping Blinkit open 90s for checkout …")
            time.sleep(90)

        finally:
            session.quit()