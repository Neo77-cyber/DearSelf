import os
import random
import pytest
from playwright.sync_api import Page, expect

BASE_URL = os.getenv("BASE_URL", "https://dear-self.onrender.com")
TEST_PHONE = os.getenv("E2E_TEST_PHONE")
TEST_PASSWORD = os.getenv("E2E_TEST_PASSWORD")
TEST_EMAIL = os.getenv("E2E_TEST_EMAIL", "test@example.com")
MAINTENANCE_KEY = os.getenv("MAINTENANCE_KEY", "")





def use_password(page: Page):
    # The password field is tucked behind a toggle now that login is code-first.
    page.click("text=Have a password? Use it instead")


def login(page: Page):

    page.goto(f"{BASE_URL}/login/")
    page.fill("[name=identifier]", TEST_PHONE)
    use_password(page)
    page.fill("[name=password]", TEST_PASSWORD)
    page.click("[type=submit]")

    try:
        expect(page).to_have_url(f"{BASE_URL}/habits/", timeout=15000)
    except Exception:
        body = page.locator("body").inner_text()
        pytest.fail(f"Login failed.\nURL: {page.url}\nPage content:\n{body[:800]}")


# ================================================================
# PAGE LOAD TESTS
# ================================================================


def test_registration_page_loads(page: Page):
    
    page.goto(BASE_URL)
    expect(page.locator("[name=identifier]")).to_be_visible()
    expect(page.locator("[name=password]")).to_have_count(0)
    expect(page.get_by_role("radio").first).to_be_attached()
    expect(page.locator("button[type=submit]")).to_be_visible()


def test_habit_choice_options_match_backend(page: Page):
    
    page.goto(BASE_URL)
    values = page.eval_on_selector_all(
        "input[name='habit_choice']", "els => els.map(e => e.value)"
    )
    valid_keys = [
        "DAILY PRAYERS",
        "WORK OUT",
        "EXAM PREPARATION",
        "GAMBLING",
        "WEED SOBER",
        "LATE NIGHT EATING",
        "BUY BUY",
        "ALCOHOL SOBER",
        "something-else",
    ]
    for value in values:
        assert value in valid_keys, f"Radio value '{value}' not in HABIT_CHOICES"


def test_login_page_loads(page: Page):
    
    page.goto(f"{BASE_URL}/login/")
    expect(page.locator("[name=identifier]")).to_be_visible()
    expect(page.locator("button[type=submit]")).to_be_visible()


def test_unauthenticated_redirected_from_habits(page: Page):
    """Test that unauthenticated users can't access dashboard"""
    page.goto(f"{BASE_URL}/habits/")
    expect(page).to_have_url(f"{BASE_URL}/login/?next=/habits/")


# ================================================================
# REGISTRATION FLOW TESTS
# ================================================================


def test_registration_requires_valid_phone(page: Page):
    
    page.goto(BASE_URL)
    page.fill("[name=identifier]", "invalid")
    page.get_by_role("radio", name="Daily Prayers").check()
    page.click("[type=submit]")

    body = page.locator("body").inner_text()
    assert "invalid" in body.lower() or "error" in body.lower()


# ================================================================
# LOGIN FLOW TESTS
# ================================================================


def test_login_full_journey(page: Page):
    
    login(page)
    expect(page).to_have_url(f"{BASE_URL}/habits/")


def test_login_wrong_password_shows_error(page: Page):
    
    
    page.goto(f"{BASE_URL}/login/")
    page.fill("[name=identifier]", TEST_PHONE)
    use_password(page)
    page.fill("[name=password]", "WrongPassword999!")
    page.click("[type=submit]")
    expect(page).to_have_url(f"{BASE_URL}/login/")
    expect(page.locator("text=Invalid")).to_be_visible()


def test_login_invalid_phone_shows_error(page: Page):
    
    page.goto(f"{BASE_URL}/login/")
    page.fill("[name=identifier]", "notaphone")
    page.click("[type=submit]")
    expect(page).to_have_url(f"{BASE_URL}/login/")


def test_authenticated_user_redirected_from_login(page: Page):
    
    login(page)
    page.goto(f"{BASE_URL}/login/")
    expect(page).to_have_url(f"{BASE_URL}/habits/")


def test_session_persists_after_refresh(page: Page):
    
    login(page)
    page.reload()
    expect(page).to_have_url(f"{BASE_URL}/habits/")
    expect(page.locator("text=/Good|Hey/").first).to_be_visible()


# ================================================================
# DASHBOARD TESTS
# ================================================================


def test_dashboard_loads_with_greeting(page: Page):
    
    login(page)
    expect(
        page.locator("text=/Good morning|Good afternoon|Good evening|Hey/").first
    ).to_be_visible()


def test_dashboard_shows_streak(page: Page):
    
    login(page)
    streak_el = page.locator("[id^='streak-']").first
    expect(streak_el).to_be_visible(timeout=8000)


def test_dashboard_shows_total_streak(page: Page):
    
    login(page)
    total_streak = page.locator("text=/Total Streak|total streak/i")
    expect(total_streak).to_be_visible()


def test_mark_habit_done_button_visible(page: Page):
    
    login(page)
    clock_in = page.locator("button:has-text('Clock In')")
    clocked_in = page.locator("text=Clocked In")
    assert clock_in.count() > 0 or clocked_in.count() > 0, "No Clock In button found"


def test_mark_habit_done_clickable(page: Page):
    
    login(page)
    clock_in = page.locator("button:has-text('Clock In')").first

    if clock_in.count() > 0 and clock_in.is_visible():
        clock_in.click()
        
        expect(page.locator("text=Clocked In").first).to_be_visible(timeout=5000)


# ================================================================
# LOGOUT TESTS
# ================================================================


def test_logout_redirects_to_index(page: Page):
    """Test that logout works correctly"""
    login(page)

   
    logout_selectors = [
        "form[action*='logout'] button",
        "button:has-text('Logout')",
        "button:has-text('Log out')",
        "a:has-text('Logout')",
        "a[href*='logout']",
    ]

    for selector in logout_selectors:
        el = page.locator(selector)
        if el.count() > 0:
            el.first.click()
            expect(page).to_have_url(BASE_URL + "/", timeout=8000)
            
            page.goto(f"{BASE_URL}/habits/")
            expect(page).to_have_url(f"{BASE_URL}/login/?next=/habits/")
            return

    
    buttons = page.eval_on_selector_all(
        "button, a", "els => els.map(e => e.innerText.trim())"
    )
    pytest.fail(f"Logout button not found. Buttons on page: {buttons}")


# ================================================================
# SESSION & SECURITY TESTS
# ================================================================


def test_session_does_not_expire_prematurely(page: Page):
    
    login(page)

    
    page.wait_for_timeout(120000)

    
    expect(page.locator("body")).to_be_visible()
    assert "/habits/" in page.url or "/login/" not in page.url


def test_csrf_protection_active(page: Page):
    
    page.goto(f"{BASE_URL}/login/")
    csrf_input = page.locator("input[name='csrfmiddlewaretoken']")
    expect(csrf_input).to_be_attached()


# ================================================================
# HEALTH CHECK TESTS
# ================================================================


def test_health_endpoint_returns_ok(page: Page):
    
    response = page.request.get(f"{BASE_URL}/health/")
    assert response.status == 200
    assert response.text() == "ok"


def test_static_files_load(page: Page):
    
    page.goto(BASE_URL)

    
    body_bg = page.evaluate("window.getComputedStyle(document.body).backgroundColor")
    assert body_bg is not None


# ================================================================
# EDGE CASE TESTS
# ================================================================


def test_concurrent_session_doesnt_conflict(page: Page, context):
    
    page2 = context.new_page()

    login(page)

    
    page2.goto(f"{BASE_URL}/login/")
   
    expect(page).to_have_url(f"{BASE_URL}/habits/")
    page2.goto(f"{BASE_URL}/")
    assert page2.url != f"{BASE_URL}/habits/"


def test_mobile_viewport_works(page: Page):
    
    page.set_viewport_size({"width": 375, "height": 667})  
    page.goto(BASE_URL)
    expect(page.locator("[name=identifier]")).to_be_visible()
    expect(page.locator("button[type=submit]")).to_be_visible()


# ================================================================
# BROWSER COMPATIBILITY (Run with different browsers)
# ================================================================


@pytest.mark.skip(reason="Run manually with --browser=firefox")
def test_firefox_compatibility(page: Page):
    """Test in Firefox browser"""
    page.goto(BASE_URL)
    expect(page.locator("[name=identifier]")).to_be_visible()


@pytest.mark.skip(reason="Run manually with --browser=webkit")
def test_safari_compatibility(page: Page):
    """Test in Safari/WebKit browser"""
    page.goto(BASE_URL)
    expect(page.locator("[name=identifier]")).to_be_visible()
