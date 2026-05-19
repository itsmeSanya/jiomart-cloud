import os
import json
import time
from fastapi import FastAPI, Request, Form, File, UploadFile, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import sqlalchemy as sa
from sqlalchemy.orm import declarative_base, sessionmaker
from playwright.sync_api import sync_playwright, TimeoutError

# ==========================================
# 1. DATABASE & APP INIT
# ==========================================
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./profiles.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = sa.create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class DBProfile(Base):
    __tablename__ = "profiles"
    id = sa.Column(sa.Integer, primary_key=True, index=True)
    name = sa.Column(sa.String, unique=True, index=True)
    session_data = sa.Column(sa.Text)

Base.metadata.create_all(bind=engine)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# ==========================================
# 2. JIOMART PLAYWRIGHT ENGINE
# ==========================================
def parse_args(raw_text):
    items = []
    lines = raw_text.strip().split('\n')
    for arg in lines:
        if "=" not in arg:
            continue
        try:
            url, qty = arg.split("=", 1)
            items.append((url.strip(), int(qty.strip())))
        except:
            print(f"⚠️ Invalid input skipped: {arg}")
    return items

def load_profile_from_db(p, profile_name):
    db = SessionLocal()
    profile_record = db.query(DBProfile).filter(DBProfile.name == profile_name).first()
    db.close()
    
    if not profile_record:
        raise Exception(f"❌ Profile not found in database: {profile_name}")

    session_dict = json.loads(profile_record.session_data)
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    # FIXED: Added optimized flags to allow seamless execution inside headless cloud containers
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-zygote",
            "--single-process"
        ]
    )
    
    context = browser.new_context(
        storage_state=session_dict,
        user_agent=user_agent,
        viewport={"width": 1280, "height": 800}
    )

    context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
    context.route("**/*.{png,jpg,jpeg,webp,svg,gif,woff,woff2,mp4,webm}", lambda route: route.abort())

    page = context.new_page()
    print(f"✅ Loaded profile from Cloud DB: {profile_name}")
    return browser, context, page

def wait_success_or_retry(page, button_type):
    for attempt in range(3):
        try:
            success_toast = page.locator("text=/Added to cart|Items updated successfully/i").first
            success_toast.wait_for(state="visible", timeout=10000)
            success_toast.wait_for(state="hidden", timeout=5000) 
            return True
        except TimeoutError:
            try:
                error_toast = page.locator("text=/Nothing updated/i").first
                if error_toast.is_visible(timeout=2000): 
                    if button_type == "add":
                        page.locator("button:has-text('Add to Cart')").first.click()
                    else:
                        page.locator("svg[data-iconname='ic_add']").first.click()
            except Exception:
                pass 
    return False

def run_flow(page, url, qty):
    print(f"\n🚀 PRODUCT → {url} | QTY → {qty}")
    page.goto(url, wait_until="domcontentloaded")

    add_btn = page.locator("button:has-text('Add to Cart')").first
    page.wait_for_function("""
        () => {
            const btns = Array.from(document.querySelectorAll("button"));
            const btn = btns.find(b => b.innerText.includes("Add to Cart"));
            return btn && !btn.disabled && btn.offsetParent !== null;
        }
    """, timeout=20000)
    
    add_btn.click()

    if not wait_success_or_retry(page, "add"):
        print("❌ Failed adding first quantity")
        return False

    for _ in range(qty - 1):
        plus = page.locator("svg[data-iconname='ic_add']").first
        plus.wait_for(state="visible", timeout=10000)
        plus.click()

        if not wait_success_or_retry(page, "plus"):
            print("❌ Failed increasing quantity")
            return False

    print("✅ Product added")
    return True

def process_cart_and_coupon(page, coupon_code):
    print("\n🛒 Moving to cart...")
    page.goto("https://www.jiomart.com/cart")
    page.wait_for_load_state("domcontentloaded")

    coupon_section = page.locator("div.coupons__viewAllCoupon")
    coupon_section.wait_for(state="visible", timeout=30000)

    if coupon_code and str(coupon_code).strip() != "":
        print(f"💳 Applying coupon: {coupon_code}")
        coupon_section.click()
        page.locator("div.modal-common__mcBody").wait_for(state="visible", timeout=20000)
        
        input_box = page.locator("input[aria-label='Coupon Code']")
        input_box.wait_for(state="visible", timeout=5000)
        input_box.click()
        input_box.fill(coupon_code)

        page.locator("div.coupons__couponDrawer button:has-text('Apply')").first.click()
        page.locator("div.coupons__couponModalMain").wait_for(state="hidden", timeout=15000)
    else:
        print("ℹ️ No coupon provided")
        quick_ready = False
        for _ in range(60):
            try:
                if page.locator("text=/Quick Delivery/i").first.is_visible():
                    quick_ready = True
                    break
            except: pass
            page.wait_for_timeout(1000)

    print("💰 Clicking Pay Now...")
    pay_btn = page.locator("button:has-text('Pay now')").first
    pay_btn.wait_for(state="visible", timeout=10000)
    pay_btn.click()

    amount_visible = False
    for _ in range(60):
        try:
            amount_box = page.locator("div.j-text.j-text-body-s")
            if amount_box.filter(has_text="Amount Payable: ₹").first.is_visible():
                amount_visible = True
                break
        except: pass
        page.wait_for_timeout(500)

    if amount_visible:
        page.wait_for_timeout(5000) 
        print("💵 Selecting Cash on Delivery...")
        cod = page.locator("div.j-text.j-text-body-xs").filter(has_text="Cash on Delivery").first
        cod.wait_for(state="visible", timeout=15000)
        cod.click()

        print("➡️ Clicking Proceed...")
        proceed_btn = page.locator("button[aria-label='Proceed']:visible").last
        proceed_btn.wait_for(state="visible", timeout=10000)
        proceed_btn.click()

        track_detected = False
        for _ in range(120):
            try:
                if page.locator("button:has-text('Track order')").first.is_visible():
                    track_detected = True
                    break
            except: pass
            page.wait_for_timeout(1000)

        if track_detected:
            print("🎉 Order completed successfully")
            return True
        else:
            print("❌ Track order not detected")
            return False
    else:
        print("❌ Amount Payable screen not detected")
        return False

def execute_cloud_bot(profile_name, products_raw, coupon):
    try:
        items = parse_args(products_raw)
        if not items:
            print("❌ No valid products found. Exiting.")
            return

        with sync_playwright() as p:
            browser, context, page = load_profile_from_db(p, profile_name)
            print(f"⚡ Starting cloud automation for: {profile_name}")
            page.goto("https://www.jiomart.com")
            page.wait_for_load_state("domcontentloaded")

            for url, qty in items:
                run_flow(page, url, qty)

            success = process_cart_and_coupon(page, coupon)
            print(f"🏁 FINAL RESULT → {'SUCCESS' if success else 'FAILED'}")
            browser.close()
    except Exception as e:
        print(f"❌ CRITICAL ERROR: {e}")

# ==========================================
# 3. WEB SERVER ROUTES
# ==========================================
@app.get("/", response_class=HTMLResponse)
def read_root(request: Request, message: str = None):
    db = SessionLocal()
    profiles = db.query(DBProfile).all()
    db.close()
    return templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={"request": request, "profiles": profiles, "message": message}
    )

@app.post("/upload_profile")
async def upload_profile(profile_name: str = Form(...), file: UploadFile = File(...)):
    content = await file.read()
    session_json_string = content.decode("utf-8")
    
    db = SessionLocal()
    existing = db.query(DBProfile).filter(DBProfile.name == profile_name).first()
    if existing:
        existing.session_data = session_json_string
    else:
        db.add(DBProfile(name=profile_name, session_data=session_json_string))
    
    db.commit()
    db.close()
    return RedirectResponse(url="/?message=📥 Profile saved successfully to Cloud DB!", status_code=303)

@app.post("/launch")
def launch_bot(
    background_tasks: BackgroundTasks,
    selected_profile: str = Form(...),
    products_raw: str = Form(...),
    coupon: str = Form(None)
):
    background_tasks.add_task(execute_cloud_bot, selected_profile, products_raw, coupon)
    return RedirectResponse(url="/?message=🚀 Bot fired successfully! Check your account in 3 minutes.", status_code=303)

