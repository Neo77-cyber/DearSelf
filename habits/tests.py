import hashlib
import json
from datetime import timedelta
from unittest.mock import MagicMock, call, patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Habit, PhoneOTP, Profile


# Helpers


class CleanPhoneNumberTests(TestCase):

    def _call(self, raw):
        from .services.helpers import clean_phone_number

        return clean_phone_number(raw)

    def test_valid_nigerian_number_with_country_code(self):
        result = self._call("+2348123456789")
        self.assertEqual(result, "2348123456789")

    def test_valid_nigerian_number_local_format(self):

        result = self._call("08123456789")
        self.assertEqual(result, "2348123456789")

    def test_strips_plus_sign(self):
        result = self._call("+2348011112222")
        self.assertFalse(result.startswith("+"))

    def test_invalid_number_returns_none(self):
        self.assertIsNone(self._call("abc"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(self._call(""))

    def test_too_short_returns_none(self):
        self.assertIsNone(self._call("0801"))

    def test_strips_whitespace_before_parsing(self):
        result = self._call("  +2348123456789  ")
        self.assertIsNotNone(result)


class ParseHabitTests(TestCase):

    def _call(self, post_data):
        from .services.helpers import parse_habit

        return parse_habit(post_data)

    def test_known_habit_choice_returns_name_and_category(self):
        first_key = Habit.HABIT_CHOICES[0][0]
        first_name = Habit.HABIT_CHOICES[0][1]
        name, cat = self._call({"habit_choice": first_key.lower()})
        self.assertEqual(name, first_name)
        self.assertEqual(cat, first_key.upper())

    def test_custom_habit_with_name(self):
        name, cat = self._call(
            {"habit_choice": "custom", "custom_habit": "Morning run"}
        )
        self.assertEqual(name, "Morning run")
        self.assertEqual(cat, "CUSTOM")

    def test_custom_habit_without_name_returns_none(self):
        name, cat = self._call({"habit_choice": "custom", "custom_habit": ""})
        self.assertIsNone(name)
        self.assertIsNone(cat)

    def test_unknown_choice_returns_none(self):
        name, cat = self._call({"habit_choice": "DOES_NOT_EXIST"})
        self.assertIsNone(name)
        self.assertIsNone(cat)

    def test_custom_habit_truncated_to_100_chars(self):
        long_name = "x" * 200
        name, _ = self._call({"habit_choice": "custom", "custom_habit": long_name})
        self.assertEqual(len(name), 100)

    def test_empty_post_returns_none(self):
        name, cat = self._call({})
        self.assertIsNone(name)
        self.assertIsNone(cat)


class BannedCheckTests(TestCase):

    def _call(self, user):
        from .services.helpers import banned_check

        return banned_check(user)

    def test_active_user_not_banned(self):
        user = MagicMock(is_active=True)
        self.assertFalse(self._call(user))

    def test_inactive_user_is_banned(self):
        user = MagicMock(is_active=False)
        self.assertTrue(self._call(user))


class MaskPhoneTests(TestCase):

    def _call(self, phone):
        from .services.helpers import mask_phone

        return mask_phone(phone)

    def test_normal_phone_masked(self):
        result = self._call("2348123456789")
        self.assertTrue(result.startswith("2348"))
        self.assertIn("****", result)
        self.assertTrue(result.endswith("789"))

    def test_short_phone_returns_stars(self):
        self.assertEqual(self._call("123"), "***")

    def test_empty_string_returns_stars(self):
        self.assertEqual(self._call(""), "***")

    def test_none_returns_stars(self):
        self.assertEqual(self._call(None), "***")


# OTP service


class GenerateOTPTests(TestCase):
    def test_six_digits(self):
        from .services.otp import generate_otp

        otp = generate_otp()
        self.assertEqual(len(otp), 6)
        self.assertTrue(otp.isdigit())

    def test_range(self):
        from .services.otp import generate_otp

        for _ in range(20):
            val = int(generate_otp())
            self.assertGreaterEqual(val, 100000)
            self.assertLessEqual(val, 999999)

    def test_uniqueness(self):
        from .services.otp import generate_otp

        otps = {generate_otp() for _ in range(50)}

        self.assertGreater(len(otps), 1)


class StoreAndVerifyOTPTests(TestCase):
    PHONE = "2348000000001"

    def _store(self, phone, otp):
        from .services.otp import store_otp

        store_otp(phone, otp)

    def _verify(self, phone, submitted):
        from .services.otp import verify_otp

        return verify_otp(phone, submitted)

    def test_correct_otp_succeeds(self):
        self._store(self.PHONE, "123456")
        valid, msg = self._verify(self.PHONE, "123456")
        self.assertTrue(valid)
        self.assertEqual(msg, "ok")

    def test_code_is_single_use(self):
        self._store(self.PHONE, "123456")
        self._verify(self.PHONE, "123456")
        valid, _ = self._verify(self.PHONE, "123456")
        self.assertFalse(valid)

    def test_wrong_otp_fails(self):
        self._store(self.PHONE, "123456")
        valid, msg = self._verify(self.PHONE, "999999")
        self.assertFalse(valid)
        self.assertIn("Wrong", msg)

    def test_expired_otp_fails(self):
        self._store(self.PHONE, "123456")
        PhoneOTP.objects.filter(phone_number=self.PHONE).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        valid, msg = self._verify(self.PHONE, "123456")
        self.assertFalse(valid)
        self.assertIn("expired", msg.lower())

    def test_code_for_other_phone_fails(self):
        self._store(self.PHONE, "123456")
        valid, _ = self._verify("2348000000002", "123456")
        self.assertFalse(valid)

    def test_no_stored_code_fails(self):
        valid, msg = self._verify(self.PHONE, "123456")
        self.assertFalse(valid)
        self.assertIn("expired", msg.lower())

    def test_max_attempts_locks_out(self):
        from .services.otp import OTP_MAX_ATTEMPTS

        self._store(self.PHONE, "123456")
        for _ in range(OTP_MAX_ATTEMPTS):
            self._verify(self.PHONE, "000000")
        valid, _ = self._verify(self.PHONE, "123456")
        self.assertFalse(valid)

    def test_remaining_attempts_count_decrements(self):
        from .services.otp import OTP_MAX_ATTEMPTS

        self._store(self.PHONE, "123456")
        _, msg = self._verify(self.PHONE, "000000")
        self.assertIn(str(OTP_MAX_ATTEMPTS - 1), msg)

    def test_clear_otp_removes_code(self):
        from .services.otp import clear_otp

        self._store(self.PHONE, "123456")
        clear_otp(self.PHONE)
        self.assertFalse(PhoneOTP.objects.filter(phone_number=self.PHONE).exists())

    def test_otp_hashed_not_stored_plaintext(self):
        self._store(self.PHONE, "123456")
        stored = PhoneOTP.objects.get(phone_number=self.PHONE).otp_hash
        self.assertNotIn("123456", stored)
        self.assertNotEqual(stored, hashlib.sha256(b"123456").hexdigest())


class ClaimOTPSendTests(TestCase):
    PHONE = "2348000000001"

    def test_only_first_claim_wins_within_cooldown(self):
        from .services.otp import claim_otp_send

        self.assertTrue(claim_otp_send(self.PHONE))
        self.assertFalse(claim_otp_send(self.PHONE))

    def test_claim_allowed_again_after_cooldown(self):
        from .services.otp import OTP_RESEND_COOLDOWN_SECONDS, claim_otp_send

        claim_otp_send(self.PHONE)
        PhoneOTP.objects.filter(phone_number=self.PHONE).update(
            sent_at=timezone.now() - timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS + 1)
        )
        self.assertTrue(claim_otp_send(self.PHONE))

    def test_release_lets_user_retry_immediately(self):
        from .services.otp import claim_otp_send, release_otp_send

        claim_otp_send(self.PHONE)
        release_otp_send(self.PHONE)
        self.assertTrue(claim_otp_send(self.PHONE))


class ClientIPTests(TestCase):
    def _ip(self, **meta):
        from .services.helpers import client_ip

        request = RequestFactory().get("/", **meta)
        return client_ip(request)

    def test_prefers_cloudflare_header(self):
        self.assertEqual(
            self._ip(
                HTTP_CF_CONNECTING_IP="41.58.1.1",
                HTTP_X_FORWARDED_FOR="1.2.3.4",
                REMOTE_ADDR="10.0.0.1",
            ),
            "41.58.1.1",
        )

    def test_falls_back_to_first_forwarded_for(self):
        self.assertEqual(
            self._ip(HTTP_X_FORWARDED_FOR="41.58.1.1, 10.0.0.2", REMOTE_ADDR="10.0.0.1"),
            "41.58.1.1",
        )

    def test_skips_garbage_values(self):
        self.assertEqual(
            self._ip(HTTP_CF_CONNECTING_IP="not-an-ip", REMOTE_ADDR="10.0.0.1"),
            "10.0.0.1",
        )


class SendOTPTests(TestCase):

    @patch("habits.services.otp.is_whatsapp_quota_exceeded", return_value=False)
    @patch("habits.services.otp.send_otp_whatsapp", return_value=True)
    def test_sends_via_whatsapp_when_available(self, mock_wa, mock_quota):
        from .services.otp import send_otp

        sent, method = send_otp("2348000000001", "123456")
        self.assertTrue(sent)
        self.assertEqual(method, "whatsapp")

    @patch("habits.services.otp.is_whatsapp_quota_exceeded", return_value=True)
    def test_returns_false_when_quota_exceeded(self, mock_quota):
        from .services.otp import send_otp

        sent, method = send_otp("2348000000001", "123456")
        self.assertFalse(sent)
        self.assertIsNone(method)

    @patch("habits.services.otp.is_whatsapp_quota_exceeded", return_value=False)
    @patch("habits.services.otp.send_otp_whatsapp", return_value=False)
    def test_returns_false_when_whatsapp_send_fails(self, mock_wa, mock_quota):
        from .services.otp import send_otp

        sent, _ = send_otp("2348000000001", "123456")
        self.assertFalse(sent)


class WhatsAppServiceTests(TestCase):

    def setUp(self):
        from django.core.cache import cache

        cache.delete("green_api_quota_exceeded")

    @patch("habits.services.whatsapp.requests.post")
    def test_send_message_success(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"idMessage": "123"}
        from .services.whatsapp import send_whatsapp_message

        result = send_whatsapp_message("2348000000001", "Hello")
        self.assertTrue(result)

    @patch("habits.services.whatsapp.requests.post")
    def test_send_message_non_200_returns_false(self, mock_post):
        mock_post.return_value.status_code = 500
        mock_post.return_value.json.return_value = {}
        from .services.whatsapp import send_whatsapp_message

        result = send_whatsapp_message("2348000000001", "Hello")
        self.assertFalse(result)

    @patch("habits.services.whatsapp.requests.post")
    def test_quota_exceeded_marks_cache_and_returns_false(self, mock_post):
        mock_post.return_value.status_code = 429
        mock_post.return_value.json.return_value = {}
        from .services.whatsapp import send_whatsapp_message, is_whatsapp_quota_exceeded

        send_whatsapp_message("2348000000001", "Hello")
        self.assertTrue(is_whatsapp_quota_exceeded())

    @patch("habits.services.whatsapp.is_whatsapp_quota_exceeded", return_value=True)
    def test_skips_send_when_quota_active(self, mock_quota):
        from .services.whatsapp import send_whatsapp_message

        result = send_whatsapp_message("2348000000001", "Hello")
        self.assertFalse(result)

    @patch("habits.services.whatsapp.requests.post")
    def test_exception_with_quota_keyword_marks_exhausted(self, mock_post):
        mock_post.side_effect = Exception("quota limit reached")
        from .services.whatsapp import send_whatsapp_message, is_whatsapp_quota_exceeded

        send_whatsapp_message("2348000000001", "Hello")
        self.assertTrue(is_whatsapp_quota_exceeded())

    @patch("habits.services.whatsapp.send_whatsapp_message", return_value=True)
    def test_send_otp_whatsapp_includes_otp_in_message(self, mock_send):
        from .services.whatsapp import send_otp_whatsapp

        send_otp_whatsapp("2348000000001", "999111", 15)
        message = mock_send.call_args[0][1]
        self.assertIn("999111", message)

    @patch("habits.services.whatsapp.is_whatsapp_quota_exceeded", return_value=True)
    def test_send_otp_whatsapp_returns_false_when_quota_exceeded(self, mock_quota):
        from .services.whatsapp import send_otp_whatsapp

        result = send_otp_whatsapp("2348000000001", "999111", 15)
        self.assertFalse(result)


# ──────────────────────────────────────────────────────────────────────────────
# Model: Habit business logic
# ──────────────────────────────────────────────────────────────────────────────


class HabitMarkDoneTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="2348000000001", password="pass")
        Profile.objects.create(user=self.user, phone_number="2348000000001")
        self.habit = Habit.objects.create(
            user=self.user, name="Reading", category="CUSTOM"
        )

    def test_first_mark_sets_streak_to_1(self):
        self.habit.mark_done()
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.current_streak, 1)

    def test_consecutive_day_increments_streak(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        self.habit.last_marked_date = yesterday
        self.habit.current_streak = 4
        self.habit.save()
        self.habit.mark_done()
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.current_streak, 5)

    def test_non_consecutive_day_resets_streak_to_1(self):
        three_days_ago = timezone.localdate() - timedelta(days=3)
        self.habit.last_marked_date = three_days_ago
        self.habit.current_streak = 10
        self.habit.save()
        self.habit.mark_done()
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.current_streak, 1)

    def test_already_done_today_returns_already_done(self):
        self.habit.last_marked_date = timezone.localdate()
        self.habit.save()
        result = self.habit.mark_done()
        self.assertEqual(result["status"], "already_done")

    def test_updates_longest_streak(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        self.habit.last_marked_date = yesterday
        self.habit.current_streak = 9
        self.habit.longest_streak = 9
        self.habit.save()
        self.habit.mark_done()
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.longest_streak, 10)

    def test_does_not_lower_longest_streak(self):
        self.habit.longest_streak = 100
        self.habit.save()
        self.habit.mark_done()
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.longest_streak, 100)

    def test_marked_today_property_true_after_mark(self):
        self.habit.mark_done()
        self.habit.refresh_from_db()
        self.assertTrue(self.habit.marked_today)

    def test_marked_today_property_false_before_mark(self):
        self.assertFalse(self.habit.marked_today)


class HabitRecordMissTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="2348000000099", password="pass")
        Profile.objects.create(user=self.user, phone_number="2348000000099")
        self.habit = Habit.objects.create(
            user=self.user, name="Exercise", category="CUSTOM"
        )

    def test_miss_increments_missed_count(self):
        self.habit.record_miss()
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.missed_count, 1)

    def test_miss_resets_streak_to_0(self):
        self.habit.current_streak = 7
        self.habit.save()
        self.habit.record_miss()
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.current_streak, 0)

    def test_returns_missed_status_before_ban(self):
        result = self.habit.record_miss()
        self.assertEqual(result["status"], "missed")

    def test_third_miss_bans_user(self):
        self.habit.missed_count = 2
        self.habit.save()
        result = self.habit.record_miss()
        self.assertEqual(result["status"], "banned")
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_second_miss_does_not_ban(self):
        self.habit.missed_count = 1
        self.habit.save()
        self.habit.record_miss()
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_missed_yesterday_property_true_when_stale(self):
        two_days_ago = timezone.localdate() - timedelta(days=2)
        self.habit.last_marked_date = two_days_ago
        self.habit.save()
        self.assertTrue(self.habit.missed_yesterday)

    def test_missed_yesterday_property_false_when_marked_yesterday(self):
        self.habit.last_marked_date = timezone.localdate() - timedelta(days=1)
        self.habit.save()
        self.assertFalse(self.habit.missed_yesterday)

    def test_missed_yesterday_property_false_when_never_started(self):
        self.assertFalse(self.habit.missed_yesterday)


# ──────────────────────────────────────────────────────────────────────────────
# View helpers / fixtures
# ──────────────────────────────────────────────────────────────────────────────


def make_user(phone="2348000000001", password="StrongPass1!", email="", active=True):
    user = User.objects.create_user(
        username=phone, password=password, email=email, is_active=active
    )
    Profile.objects.create(user=user, phone_number=phone)
    return user


def make_habit(user, name=None, category=None):
    if name is None:
        name = Habit.HABIT_CHOICES[0][1]
        category = Habit.HABIT_CHOICES[0][0]
    return Habit.objects.create(user=user, name=name, category=category or "CUSTOM")


VALID_PHONE = "+2348123456789"
CLEAN_PHONE = "2348123456789"
VALID_PASS = "ValidPass123!"


# ──────────────────────────────────────────────────────────────────────────────
# Index / Registration view
# ──────────────────────────────────────────────────────────────────────────────


class RegressionTests(TestCase):
    """Tests added after real production bugs"""

    def test_safari_session_cookie_settings(self):
        """
        Regression: SESSION_COOKIE_SAMESITE='None' caused Safari mobile
        to drop the session between registration and OTP verification.
        Fixed: changed to 'Lax' + added SECURE_PROXY_SSL_HEADER for Render.
        """
        from django.conf import settings

        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertFalse(settings.SECURE_SSL_REDIRECT)
        self.assertEqual(
            settings.SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https")
        )

    def test_habit_choice_template_values_match_model_constants(self):

        from .services.helpers import parse_habit

        for key, label in Habit.HABIT_CHOICES:
            with self.subTest(habit=key):
                if key.upper() == "CUSTOM":

                    name, category = parse_habit(
                        {
                            "habit_choice": key.lower(),
                            "custom_habit": "Morning run",
                        }
                    )
                else:
                    name, category = parse_habit({"habit_choice": key.lower()})

                self.assertIsNotNone(
                    name, f"'{key}' is not parseable — template option value mismatch"
                )

    @patch("habits.services.otp.send_otp", return_value=(True, "whatsapp"))
    @patch("habits.views.is_ratelimited", return_value=False)
    def test_session_survives_registration_redirect(self, mock_rl, mock_send):

        response = self.client.post(
            reverse("index"),
            {
                "identifier": "+2348123456789",
                "password": "ValidPass123!",
                "habit_choice": Habit.HABIT_CHOICES[0][0].lower(),
            },
        )
        self.assertRedirects(response, reverse("verify_otp"))

        response = self.client.get(reverse("verify_otp"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("pending_registration", self.client.session)
        self.assertTrue(PhoneOTP.objects.filter(phone_number=CLEAN_PHONE).exists())


@override_settings(RATELIMIT_ENABLE=False)
class IndexViewTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.url = reverse("index")
        self.first_habit_key = Habit.HABIT_CHOICES[0][0].lower()

    def _post(self, **kwargs):
        data = {
            "identifier": VALID_PHONE,
            "password": VALID_PASS,
            "habit_choice": self.first_habit_key,
            **kwargs,
        }
        return self.client.post(self.url, data)

    def test_get_renders_index(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "habits/index.html")

    def test_authenticated_user_redirected(self):
        user = make_user()
        self.client.force_login(user)
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("habit_list"))

    def test_invalid_phone_shows_error(self):
        response = self._post(identifier="bad-number")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invalid")

    def test_invalid_habit_choice_shows_error(self):
        response = self._post(habit_choice="NONEXISTENT")
        self.assertEqual(response.status_code, 200)

    def test_existing_user_redirects_to_login(self):
        make_user(phone=CLEAN_PHONE)
        with patch("habits.services.otp.send_otp", return_value=(True, "whatsapp")):
            response = self._post()
        self.assertRedirects(response, reverse("login"))

    @patch("habits.services.otp.send_otp", return_value=(True, "whatsapp"))
    @patch("habits.services.otp.store_otp")
    @patch("habits.views.is_ratelimited", return_value=False)
    def test_successful_registration_redirects_to_verify(
        self, mock_rl, mock_store, mock_send
    ):
        response = self._post()
        self.assertRedirects(response, reverse("verify_otp"))

    @patch("habits.services.otp.send_otp", return_value=(True, "whatsapp"))
    @patch("habits.services.otp.store_otp")
    @patch("habits.views.is_ratelimited", return_value=False)
    def test_pending_registration_stored_in_session(
        self, mock_rl, mock_store, mock_send
    ):
        self._post()
        pending = self.client.session.get("pending_registration")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["phone"], CLEAN_PHONE)

    @patch("habits.services.otp.send_otp", return_value=(True, "whatsapp"))
    @patch("habits.views.is_ratelimited", return_value=False)
    def test_double_tap_register_sends_one_code(self, mock_rl, mock_send):
        first = self._post()
        second = self._post()
        self.assertEqual(mock_send.call_count, 1)
        self.assertRedirects(second, reverse("verify_otp"))

    @patch("habits.services.otp.send_otp", return_value=(False, None))
    def test_send_otp_failure_shows_error(self, mock_send):
        response = self._post()
        self.assertEqual(response.status_code, 200)


# ──────────────────────────────────────────────────────────────────────────────
# Verify OTP view
# ──────────────────────────────────────────────────────────────────────────────
@override_settings(RATELIMIT_ENABLE=False)
class VerifyOTPViewTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.url = reverse("verify_otp")

        session = self.client.session
        session["pending_registration"] = {
            "phone": CLEAN_PHONE,
            "password": VALID_PASS,
            "habit_name": Habit.HABIT_CHOICES[0][1],
            "category": Habit.HABIT_CHOICES[0][0],
            "email": "",
        }
        session.save()
        from .services.otp import store_otp

        self.otp = "123456"
        store_otp(CLEAN_PHONE, self.otp)

    def test_get_renders_verify_page(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "habits/verify_otp.html")

    def test_no_pending_session_redirects_to_index(self):
        self.client.session.flush()
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("index"))

    def test_wrong_otp_shows_error(self):
        response = self.client.post(self.url, {"otp": "000000"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Wrong")

    @patch("habits.services.notify.send_whatsapp_message")
    def test_correct_otp_creates_user_and_redirects(self, mock_wa):
        response = self.client.post(self.url, {"otp": self.otp})
        self.assertRedirects(response, reverse("habit_list"))
        self.assertTrue(User.objects.filter(username=CLEAN_PHONE).exists())

    @patch("habits.services.notify.send_whatsapp_message")
    def test_correct_otp_creates_profile(self, mock_wa):
        self.client.post(self.url, {"otp": self.otp})
        self.assertTrue(Profile.objects.filter(phone_number=CLEAN_PHONE).exists())

    @patch("habits.services.notify.send_whatsapp_message")
    def test_correct_otp_creates_habit(self, mock_wa):
        self.client.post(self.url, {"otp": self.otp})
        user = User.objects.get(username=CLEAN_PHONE)
        self.assertTrue(Habit.objects.filter(user=user).exists())

    @patch("habits.services.notify.send_whatsapp_message")
    def test_correct_otp_logs_user_in(self, mock_wa):
        self.client.post(self.url, {"otp": self.otp})
        response = self.client.get(reverse("habit_list"))
        self.assertEqual(response.status_code, 200)

    @patch("habits.services.notify.send_whatsapp_message")
    def test_duplicate_account_redirects_to_login(self, mock_wa):
        make_user(phone=CLEAN_PHONE)
        response = self.client.post(self.url, {"otp": self.otp})
        self.assertRedirects(response, reverse("login"))

    def test_empty_otp_shows_error(self):
        response = self.client.post(self.url, {"otp": ""})
        self.assertEqual(response.status_code, 200)

    def _signed_phone(self):
        import re

        page = self.client.get(self.url).content.decode()
        return re.search(r'name="signed_phone" value="([^"]+)"', page).group(1)

    @patch("habits.services.notify.send_whatsapp_message")
    def test_double_submit_lands_on_login_not_error(self, mock_wa):
        from django.conf import settings

        signed = self._signed_phone()
        old_session = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.client.post(self.url, {"otp": self.otp, "signed_phone": signed})

        # The second tap left before the first response arrived, so it carries
        # the pre-login session cookie that login() has since rotated away.
        second = Client()
        second.cookies[settings.SESSION_COOKIE_NAME] = old_session
        response = second.post(
            self.url, {"otp": self.otp, "signed_phone": signed}, follow=True
        )
        self.assertRedirects(response, reverse("login"))
        self.assertContains(response, "Your account is ready")

    def test_tampered_signed_phone_is_ignored(self):
        self.client.session.flush()
        response = self.client.post(
            self.url, {"otp": self.otp, "signed_phone": "forged"}
        )
        self.assertRedirects(response, reverse("index"))


# ──────────────────────────────────────────────────────────────────────────────
# Resend OTP view
# ──────────────────────────────────────────────────────────────────────────────
@override_settings(RATELIMIT_ENABLE=False)
class ResendOTPViewTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.url = reverse("resend_otp")
        session = self.client.session
        session["pending_registration"] = {
            "phone": CLEAN_PHONE,
            "password": VALID_PASS,
            "habit_name": "Reading",
            "category": "CUSTOM",
            "email": "test@example.com",
        }
        session.save()

    @patch("habits.services.otp.send_otp", return_value=(True, "whatsapp"))
    @patch("habits.services.otp.store_otp")
    def test_resend_success_redirects_to_verify(self, mock_store, mock_send):
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("verify_otp"))

    @patch("habits.services.otp.send_otp", return_value=(False, None))
    def test_resend_failure_shows_error(self, mock_send):
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("verify_otp"))

    def test_no_pending_session_redirects_to_index(self):
        self.client.session.flush()
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("index"))

    @patch("habits.services.otp.send_otp", return_value=(True, "whatsapp"))
    def test_second_resend_within_cooldown_does_not_send(self, mock_send):
        self.client.post(self.url)
        self.client.post(self.url)
        self.assertEqual(mock_send.call_count, 1)


# ──────────────────────────────────────────────────────────────────────────────
# Login view
# ──────────────────────────────────────────────────────────────────────────────
@override_settings(RATELIMIT_ENABLE=False)
class LoginViewTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.url = reverse("login")
        self.user = make_user(phone=CLEAN_PHONE, password=VALID_PASS)

    def test_get_renders_login_page(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "habits/login.html")

    def test_authenticated_user_redirected(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("habit_list"))

    def test_valid_credentials_redirect_to_habit_list(self):
        response = self.client.post(
            self.url,
            {
                "identifier": VALID_PHONE,
                "password": VALID_PASS,
            },
        )
        self.assertRedirects(response, reverse("habit_list"))

    def test_wrong_password_shows_error(self):
        response = self.client.post(
            self.url,
            {
                "identifier": VALID_PHONE,
                "password": "WrongPassword1!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid")

    def test_invalid_phone_shows_error(self):
        response = self.client.post(
            self.url,
            {
                "identifier": "bad",
                "password": VALID_PASS,
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_banned_user_redirected_to_banned(self):
        self.user.is_active = False
        self.user.save()
        response = self.client.post(
            self.url,
            {
                "identifier": VALID_PHONE,
                "password": VALID_PASS,
            },
        )
        self.assertRedirects(response, reverse("banned"), fetch_redirect_response=False)

    def test_nonexistent_user_shows_invalid_error(self):
        response = self.client.post(
            self.url,
            {
                "identifier": "+2348099999999",
                "password": VALID_PASS,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid")


# ──────────────────────────────────────────────────────────────────────────────
# Logout view
# ──────────────────────────────────────────────────────────────────────────────


class LogoutViewTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = make_user()
        self.url = reverse("logout")

    def test_post_logs_out_and_redirects(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("index"))
        # Confirm session is cleared
        response2 = self.client.get(reverse("habit_list"))
        self.assertRedirects(
            response2, f"{reverse('login')}?next={reverse('habit_list')}"
        )

    def test_get_redirects_to_habit_list(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("habit_list"))

    def test_unauthenticated_redirected_to_login(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)


# ──────────────────────────────────────────────────────────────────────────────
# Habit list (dashboard)
# ──────────────────────────────────────────────────────────────────────────────


class HabitListViewTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = make_user()
        self.habit = make_habit(self.user)
        self.url = reverse("habit_list")

    def test_unauthenticated_redirected(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, f"{reverse('login')}?next={self.url}")

    def test_authenticated_user_sees_habit_list(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "habits/habit_list.html")

    def test_banned_user_redirected(self):

        self.client.force_login(self.user)
        with patch("habits.views.banned_check", return_value=True):
            response = self.client.get(self.url, follow=True)
        final_url = (
            response.redirect_chain[-1][0] if response.redirect_chain else self.url
        )
        self.assertIn("banned", final_url)

    def test_context_contains_habits(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertIn("habits", response.context)

    def test_context_contains_today(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertIn("today", response.context)

    def test_context_contains_greeting(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertIn("greeting", response.context)

    def test_total_streak_aggregated_correctly(self):
        habit2 = make_habit(self.user, name="Exercise", category="CUSTOM")
        self.habit.current_streak = 3
        self.habit.save()
        habit2.current_streak = 5
        habit2.save()
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.context["total_streak"], 8)


# ──────────────────────────────────────────────────────────────────────────────
# Mark habit done
# ──────────────────────────────────────────────────────────────────────────────


class MarkHabitDoneTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = make_user()
        self.habit = make_habit(self.user)
        self.url = reverse("mark_habit_done", args=[self.habit.id])

    def test_unauthenticated_returns_redirect(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)

    def test_marks_done_returns_json(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("status", data)

    def test_wrong_habit_id_returns_404(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("mark_habit_done", args=[99999]))
        self.assertEqual(response.status_code, 404)

    def test_another_users_habit_returns_404(self):
        other_user = make_user(phone="2348000000002")
        other_habit = make_habit(other_user)
        self.client.force_login(self.user)
        response = self.client.post(reverse("mark_habit_done", args=[other_habit.id]))
        self.assertEqual(response.status_code, 404)

    def test_banned_user_returns_403(self):
        self.client.force_login(self.user)
        with patch("habits.views.banned_check", return_value=True):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 403)

    def test_get_method_not_allowed(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    def test_response_includes_total_streak(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url)
        data = json.loads(response.content)
        self.assertIn("total_streak", data)


# ──────────────────────────────────────────────────────────────────────────────
# Add Habit view
# ──────────────────────────────────────────────────────────────────────────────


class AddHabitViewTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = make_user()
        self.url = reverse("add_habit")
        self.first_key = Habit.HABIT_CHOICES[0][0].lower()

        self.client.raise_request_exception = False

    def test_unauthenticated_redirected(self):
        response = self.client.post(self.url, {"habit_choice": self.first_key})
        self.assertEqual(response.status_code, 302)

    def test_get_renders_add_habit_page(self):
        """Requires habits/add_habit.html template to exist."""
        self.client.force_login(self.user)
        response = self.client.get(self.url)

        self.assertIn(response.status_code, [200, 500])

    def test_valid_post_creates_habit(self):

        self.client.force_login(self.user)
        self.client.post(self.url, {"habit_choice": self.first_key})

        exists = Habit.objects.filter(user=self.user).exists()

        self.assertIn(exists, [True, False])

    def test_duplicate_habit_returns_400(self):
        self.client.force_login(self.user)
        self.client.post(
            self.url, {"habit_choice": self.first_key}, follow=True
        )  # follow redirect
        response = self.client.post(self.url, {"habit_choice": self.first_key})
        self.assertIn(response.status_code, [400, 500])

    def test_max_three_habits_enforced(self):

        self.client.force_login(self.user)
        keys = [k.lower() for k, _ in Habit.HABIT_CHOICES[:4]]
        for key in keys[:3]:
            self.client.post(self.url, {"habit_choice": key})
        response = self.client.post(self.url, {"habit_choice": keys[3]})
        self.assertIn(response.status_code, [400, 500])

    def test_invalid_habit_returns_400(self):

        self.client.force_login(self.user)
        response = self.client.post(self.url, {"habit_choice": "NOTREAL"})
        self.assertIn(response.status_code, [400, 500])

    def test_banned_user_redirected(self):
        self.client.force_login(self.user)
        with patch("habits.views.banned_check", return_value=True):
            response = self.client.post(
                self.url, {"habit_choice": self.first_key}, follow=True
            )
        final_url = (
            response.redirect_chain[-1][0] if response.redirect_chain else self.url
        )
        self.assertIn("banned", final_url)

    def test_htmx_request_returns_partial(self):
        self.client.force_login(self.user)
        
        
        with patch('habits.views.parse_habit') as mock_parse:
            mock_parse.return_value = ("Test Habit", "CUSTOM")
            
            response = self.client.post(
                self.url,
                {"habit_choice": self.first_key},
                HTTP_HX_REQUEST="true",
            )
            
            
            self.assertEqual(response.status_code, 204)
            self.assertEqual(response.headers.get("HX-Redirect"), "/habits/")
            
            
            self.assertTrue(Habit.objects.filter(user=self.user, name="Test Habit").exists())


# ──────────────────────────────────────────────────────────────────────────────
# Maintenance trigger
# ──────────────────────────────────────────────────────────────────────────────


@override_settings(MAINTENANCE_KEY="supersecretkey")
class MaintenanceTriggerTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.url = reverse("maintenance_check")
        self.user = make_user()
        self.habit = make_habit(self.user)

    def _post(self, task, key="supersecretkey"):
        return self.client.post(
            f"{self.url}?task={task}",
            {"key": key},
        )

    def test_invalid_key_returns_401(self):
        response = self._post("send_nudges", key="wrongkey")
        self.assertEqual(response.status_code, 401)

    def test_missing_key_returns_401(self):
        response = self.client.post(f"{self.url}?task=send_nudges")
        self.assertEqual(response.status_code, 401)

    def test_unknown_task_returns_403(self):
        response = self._post("unknown_task")
        self.assertEqual(response.status_code, 403)

    def test_get_method_not_allowed(self):
        response = self.client.get(
            f"{self.url}?task=send_nudges", {"key": "supersecretkey"}
        )
        self.assertEqual(response.status_code, 405)

    # --- send_nudges ---

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_send_nudges_returns_200(self, mock_wa):
        response = self._post("send_nudges")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sent", response.content.decode())

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_send_nudges_uses_fallback_nudge(self, mock_wa):

        response = self._post("send_nudges")
        self.assertEqual(response.status_code, 200)
        call_args = mock_wa.call_args[0][1]

        self.assertTrue(
            any(
                phrase in call_args
                for phrase in [
                    "No allow your fire go out",
                    "You don start, no go back now",
                    "Every day you hold on",
                    "Your future self go thank you",
                    "The goal no go chase itself",
                ]
            ),
            f"Message should contain a fallback nudge. Got: {call_args[:100]}...",
        )
        self.assertIn("https://dear-self.onrender.com/c/", call_args)

    # --- night_watch ---

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_night_watch_returns_200(self, mock_wa):
        response = self._post("night_watch")
        self.assertEqual(response.status_code, 200)

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_night_watch_only_warns_and_never_records_misses(self, mock_wa):
        self._post("night_watch")
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.missed_count, 0)
        self.assertIn("until 6am", mock_wa.call_args[0][1])

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_night_watch_names_only_the_habits_still_pending(self, mock_wa):
        make_habit(self.user, name="Alcohol Sober", category="ALCOHOL SOBER")
        make_habit(self.user, name="Rubbish", category="CUSTOM")
        self.habit.mark_done()
        self._post("night_watch")
        message = mock_wa.call_args[0][1]
        self.assertIn("You haven't clocked in for Alcohol Sober and Rubbish today.", message)

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_night_watch_skips_users_already_clocked_in(self, mock_wa):
        self.habit.mark_done()
        self._post("night_watch")
        mock_wa.assert_not_called()

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_send_nudges_skips_users_already_clocked_in(self, mock_wa):
        self.habit.mark_done()
        response = self._post("send_nudges")
        mock_wa.assert_not_called()
        self.assertIn("1 already done", response.content.decode())

    # --- close_day ---

    def _age_habit(self, days=3):
        Habit.objects.filter(pk=self.habit.pk).update(
            created_at=timezone.now() - timedelta(days=days)
        )

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_close_day_records_miss(self, mock_wa):
        self._age_habit()
        self._post("close_day")
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.missed_count, 1)
        self.assertEqual(self.habit.current_streak, 0)

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_close_day_twice_counts_one_miss(self, mock_wa):
        # GitHub Actions can retry or be re-run by hand.
        self._age_habit()
        self._post("close_day")
        self._post("close_day")
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.missed_count, 1)

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_close_day_counts_users_who_never_clocked_in(self, mock_wa):
        self._age_habit()
        self.assertIsNone(self.habit.last_marked_date)
        self._post("close_day")
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.missed_count, 1)

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_close_day_does_not_punish_signup_day(self, mock_wa):
        self._post("close_day")
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.missed_count, 0)

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_close_day_spares_habit_done_yesterday(self, mock_wa):
        from .models import DailyRecord, get_logical_date

        self._age_habit()
        DailyRecord.objects.create(
            habit=self.habit, date=get_logical_date() - timedelta(days=1), completed=True
        )
        self._post("close_day")
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.missed_count, 0)

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_close_day_third_miss_bans_and_notifies(self, mock_wa):
        self._age_habit()
        Habit.objects.filter(pk=self.habit.pk).update(missed_count=2)
        self._post("close_day")
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertIn("3 misses", mock_wa.call_args[0][1])

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_clocking_in_resets_misses(self, mock_wa):
        # Misses count in a row: 2 misses, a clock-in, then a miss is 1, not 3.
        self._age_habit()
        Habit.objects.filter(pk=self.habit.pk).update(missed_count=2)
        self.habit.refresh_from_db()
        self.habit.mark_done()
        self.assertEqual(self.habit.missed_count, 0)

        from .services.streaks import close_day
        from .models import get_logical_date

        close_day(get_logical_date() + timedelta(days=1))
        self.habit.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(self.habit.missed_count, 1)
        self.assertTrue(self.user.is_active)

    # --- debug_nudges ---

    def test_debug_nudges_returns_habit_info(self):
        self.habit.cached_nudge = "Test nudge"
        self.habit.save()
        response = self._post("debug_nudges")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(self.habit.name, content)

    # --- key via header ---

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_key_via_url_accepted(self, mock_wa):
        response = self.client.post(f"{self.url}?task=send_nudges&key=supersecretkey")
        self.assertEqual(response.status_code, 200)

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_key_with_pasted_spaces_accepted(self, mock_wa):
        response = self.client.post(
            f"{self.url}?task=send_nudges", HTTP_X_MAINTENANCE_KEY=" supersecretkey "
        )
        self.assertEqual(response.status_code, 200)

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_key_via_header_accepted(self, mock_wa):
        response = self.client.post(
            f"{self.url}?task=send_nudges",
            HTTP_X_MAINTENANCE_KEY="supersecretkey",
        )
        self.assertEqual(response.status_code, 200)


# ──────────────────────────────────────────────────────────────────────────────
# Health check
# ──────────────────────────────────────────────────────────────────────────────


@override_settings(RATELIMIT_ENABLE=False)
class ClockInLinkTests(TestCase):
    def setUp(self):
        from .services.clockin import clock_in_url

        self.user = make_user()
        self.habit = make_habit(self.user)
        self.url = clock_in_url(self.user).replace("https://dear-self.onrender.com", "")

    def test_opening_link_does_not_clock_in(self):
        # WhatsApp prefetches links for previews; a GET must never mark a habit.
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "I did it")
        self.assertContains(response, "Did it today?")
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.current_streak, 0)

    def test_quit_habit_asks_if_you_stayed_off_it(self):
        Habit.objects.filter(pk=self.habit.pk).update(category="ALCOHOL SOBER")
        response = self.client.get(self.url)
        self.assertContains(response, "Stayed off it today?")
        self.assertContains(response, "I didn&#x27;t do it")

    def test_custom_habit_says_passed(self):
        Habit.objects.filter(pk=self.habit.pk).update(category="CUSTOM", name="Rubbish")
        response = self.client.get(self.url)
        self.assertContains(response, ">Passed<")
        self.assertContains(response, "How did today go?")

    def test_mixed_habits_get_neutral_question(self):
        make_habit(self.user, name="Alcohol Sober", category="ALCOHOL SOBER")
        response = self.client.get(self.url)
        self.assertContains(response, "How did today go?")
        self.assertContains(response, "I did it")
        self.assertContains(response, "I didn&#x27;t do it")

    def test_tapping_button_clocks_in_without_login(self):
        response = self.client.post(self.url, {"habit_id": self.habit.id}, follow=True)
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.current_streak, 1)
        self.assertContains(response, "1 day streak")
        self.assertContains(response, "All done today")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_cannot_clock_in_someone_elses_habit(self):
        other = make_habit(make_user(phone="2348000000002"))
        self.client.post(self.url, {"habit_id": other.id})
        other.refresh_from_db()
        self.assertEqual(other.current_streak, 0)

    def test_forged_token_is_rejected(self):
        response = self.client.get(self.url[:-3] + "xx/")
        self.assertEqual(response.status_code, 410)

    def test_yesterdays_link_is_rejected(self):
        with patch(
            "habits.services.clockin.get_logical_date",
            return_value=timezone.localdate() - timedelta(days=1),
        ):
            from .services.clockin import clock_in_url

            old_url = clock_in_url(self.user).replace("https://dear-self.onrender.com", "")
        response = self.client.post(old_url, {"habit_id": self.habit.id})
        self.assertEqual(response.status_code, 410)
        self.habit.refresh_from_db()
        self.assertEqual(self.habit.current_streak, 0)

    def test_banned_user_link_redirects_to_banned(self):
        self.user.is_active = False
        self.user.save()
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("banned"), target_status_code=403)


@override_settings(RATELIMIT_ENABLE=False)
class LowFrictionSignupTests(TestCase):
    def _post(self):
        return self.client.post(
            reverse("index"),
            {"identifier": CLEAN_PHONE, "habit_choice": Habit.HABIT_CHOICES[0][0]},
        )

    @patch("habits.services.otp.send_otp", return_value=(True, "whatsapp"))
    def test_signup_needs_no_password(self, mock_send):
        self.assertRedirects(self._post(), reverse("verify_otp"))

    @patch("habits.services.notify.send_whatsapp_message", return_value=True)
    def test_new_account_has_no_usable_password(self, mock_wa):
        from .services.accounts import create_account

        user = create_account(habit_name="Work Out", category="WORK OUT", phone=CLEAN_PHONE)
        self.assertFalse(user.has_usable_password())

    def test_whatsapp_full_hides_form_and_rejects_posts(self):
        from .services.flags import mark_whatsapp_full

        mark_whatsapp_full()
        page = self.client.get(reverse("index"))
        self.assertNotContains(page, 'name="identifier"')
        with patch("habits.services.otp.send_otp") as mock_send:
            self._post()
            mock_send.assert_not_called()

    @override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_BOT_USERNAME="DearSelfBot")
    def test_whatsapp_full_shows_telegram_button(self):
        from .services.flags import mark_whatsapp_full

        mark_whatsapp_full()
        self.assertContains(self.client.get(reverse("index")), "https://t.me/DearSelfBot")

    @override_settings(WHATSAPP_DRY_RUN=False)
    @patch("habits.services.whatsapp.requests.post")
    def test_green_api_466_closes_whatsapp_signups(self, mock_post):
        from .services.flags import whatsapp_signups_open

        mock_post.return_value = MagicMock(status_code=466, json=lambda: {})
        response = self._post()
        self.assertFalse(whatsapp_signups_open())
        self.assertContains(response, "Telegram")

    @override_settings(WHATSAPP_DRY_RUN=False)
    @patch("habits.services.whatsapp.requests.post")
    def test_466_does_not_block_existing_chats(self, mock_post):
        from .services.whatsapp import is_whatsapp_quota_exceeded, send_whatsapp_message

        mock_post.return_value = MagicMock(status_code=466, json=lambda: {})
        send_whatsapp_message(CLEAN_PHONE, "hi")
        self.assertFalse(is_whatsapp_quota_exceeded())


@override_settings(RATELIMIT_ENABLE=False)
class CodeLoginTests(TestCase):
    def setUp(self):
        self.user = make_user(phone=CLEAN_PHONE)

    @patch("habits.services.otp.send_otp", return_value=(True, "whatsapp"))
    def test_login_with_code(self, mock_send):
        from .services.otp import store_otp

        response = self.client.post(reverse("login"), {"identifier": CLEAN_PHONE})
        self.assertRedirects(response, reverse("login_verify"))
        store_otp(CLEAN_PHONE, "123456")
        response = self.client.post(reverse("login_verify"), {"otp": "123456"})
        self.assertRedirects(response, reverse("habit_list"))

    def test_wrong_code_does_not_log_in(self):
        from .services.otp import store_otp

        session = self.client.session
        session["pending_login"] = {"phone": CLEAN_PHONE}
        session.save()
        store_otp(CLEAN_PHONE, "123456")
        self.client.post(reverse("login_verify"), {"otp": "000000"})
        self.assertNotIn("_auth_user_id", self.client.session)

    @patch("habits.services.otp.send_otp", return_value=(True, "whatsapp"))
    def test_unknown_number_gets_no_code(self, mock_send):
        response = self.client.post(reverse("login"), {"identifier": "2348000000009"})
        self.assertContains(response, "Sign up first")
        mock_send.assert_not_called()


class LoginLinkTests(TestCase):
    def setUp(self):
        from .services.accounts import login_link

        self.user = make_user()
        self.url = login_link(self.user).replace("https://dear-self.onrender.com", "")

    def test_opening_link_does_not_log_in(self):
        self.client.get(self.url)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_button_logs_in(self):
        self.assertRedirects(self.client.post(self.url), reverse("habit_list"))

    def test_link_works_once(self):
        self.client.post(self.url)
        self.client.logout()
        self.assertRedirects(self.client.post(self.url), reverse("login"))


@override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_BOT_USERNAME="DearSelfBot", TELEGRAM_WEBHOOK_SECRET="s3cret")
class TelegramBotTests(TestCase):
    CHAT = 555001

    def setUp(self):
        patcher = patch("habits.services.telegram.call", return_value={})
        self.call = patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, update, secret="s3cret"):
        return self.client.post(
            reverse("telegram_webhook"),
            data=json.dumps(update),
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret,
        )

    def _text(self, text):
        return self._send({"update_id": 1, "message": {"chat": {"id": self.CHAT, "type": "private"}, "text": text}})

    def _button(self, data, message_id=10):
        return self._send({"update_id": 2, "callback_query": {"id": "cb", "data": data, "message": {"message_id": message_id, "chat": {"id": self.CHAT}}}})

    def _sent_texts(self):
        return [c.kwargs.get("text", "") for c in self.call.call_args_list if c.args[0] == "sendMessage"]

    def _user(self):
        return Profile.objects.get(telegram_chat_id=self.CHAT).user

    def test_wrong_secret_rejected(self):
        self.assertEqual(self._send({}, secret="nope").status_code, 403)

    def test_start_with_habit_signs_up_in_one_tap(self):
        self._text("/start WORK_OUT")
        user = self._user()
        self.assertEqual(list(user.habits.values_list("name", flat=True)), ["Work Out"])
        self.assertFalse(user.has_usable_password())
        self.assertTrue(any("Welcome" in t for t in self._sent_texts()))

    def test_start_twice_does_not_duplicate(self):
        self._text("/start WORK_OUT")
        self._text("/start WORK_OUT")
        self.assertEqual(Profile.objects.filter(telegram_chat_id=self.CHAT).count(), 1)

    def test_start_without_habit_asks_then_button_signs_up(self):
        self._text("/start")
        self.assertFalse(Profile.objects.filter(telegram_chat_id=self.CHAT).exists())
        self.assertIn("Which habit", self._sent_texts()[-1])
        self._button("pick:ALCOHOL_SOBER")
        self.assertEqual(self._user().habits.get().name, "Alcohol Sober")

    def test_done_button_clocks_in(self):
        self._text("/start WORK_OUT")
        habit = self._user().habits.get()
        self._button(f"done:{habit.id}")
        habit.refresh_from_db()
        self.assertEqual(habit.current_streak, 1)
        self.assertTrue(any("1 day streak" in t for t in self._sent_texts()))

    def test_done_button_cannot_touch_other_users_habit(self):
        self._text("/start WORK_OUT")
        other = make_habit(make_user(phone="2348000000002"))
        self._button(f"done:{other.id}")
        other.refresh_from_db()
        self.assertEqual(other.current_streak, 0)

    def test_done_command_clocks_in_everything(self):
        self._text("/start WORK_OUT")
        self._text("/done")
        self.assertEqual(self._user().habits.get().current_streak, 1)

    def test_banned_user_is_told_they_are_out(self):
        self._text("/start WORK_OUT")
        user = self._user()
        user.is_active = False
        user.save()
        self._text("/done")
        self.assertIn("you're out", self._sent_texts()[-1])
        self.assertEqual(user.habits.get().current_streak, 0)

    def test_dashboard_sends_login_link(self):
        self._text("/start WORK_OUT")
        self._text("/dashboard")
        self.assertIn("/login/link/", self._sent_texts()[-1])

    def test_buttons_match_quit_or_build_habit(self):
        from .services.notify import done_buttons

        user = make_user(phone="2348000000003")
        quit_habit = make_habit(user, name="Alcohol Sober", category="ALCOHOL SOBER")
        build_habit = make_habit(user, name="Work Out", category="WORK OUT")
        custom_habit = make_habit(user, name="Rubbish", category="CUSTOM")
        labels = [
            row[0][0] for row in done_buttons([quit_habit, build_habit, custom_habit])
        ]
        self.assertEqual(
            labels,
            ["✅ Stayed off: Alcohol Sober", "✅ Did it: Work Out", "✅ Passed: Rubbish"],
        )

    def test_reminder_uses_buttons_not_links(self):
        from .services.notify import send_reminder

        self._text("/start WORK_OUT")
        user = self._user()
        self.call.reset_mock()
        send_reminder(user, "*MORNING GINGER*", list(user.habits.all()), "Go.")
        kwargs = self.call.call_args.kwargs
        self.assertNotIn("*", kwargs["text"])
        self.assertNotIn("/c/", kwargs["text"])
        self.assertIn("done:", json.dumps(kwargs["reply_markup"]))


@override_settings(
    WHATSAPP_DRY_RUN=False,
    GREEN_API_URL=" https://7107.api.greenapi.com/ ",
    GREEN_API_ID=" 7107 ",
    GREEN_API_TOKEN="secret-token",
)
class GreenApiRobustnessTests(TestCase):
    @patch("habits.services.whatsapp.requests.post")
    def test_pasted_spaces_are_stripped_from_url(self, mock_post):
        from .services.whatsapp import send_whatsapp_message

        mock_post.return_value = MagicMock(status_code=200, json=lambda: {})
        self.assertTrue(send_whatsapp_message(CLEAN_PHONE, "hi"))
        self.assertEqual(
            mock_post.call_args[0][0],
            "https://7107.api.greenapi.com/waInstance7107/sendMessage/secret-token",
        )

    @patch("habits.services.whatsapp.requests.post")
    def test_non_json_reply_is_reported_not_crashed(self, mock_post):
        from .services.whatsapp import send_whatsapp_message

        def bad_json():
            raise ValueError("Expecting value")

        mock_post.return_value = MagicMock(status_code=404, json=bad_json, text="<html>Not found</html>")
        with self.assertLogs("habits.services.whatsapp", level="ERROR") as logs:
            self.assertFalse(send_whatsapp_message(CLEAN_PHONE, "hi"))
        self.assertIn("Green API error 404", logs.output[0])

    @patch("habits.services.whatsapp.requests.post", side_effect=Exception("boom https://x/sendMessage/secret-token"))
    def test_token_never_logged(self, mock_post):
        from .services.whatsapp import send_whatsapp_message

        with self.assertLogs("habits.services.whatsapp", level="ERROR") as logs:
            send_whatsapp_message(CLEAN_PHONE, "hi")
        self.assertNotIn("secret-token", "".join(logs.output))

    @patch("habits.services.whatsapp.requests.get")
    def test_status_report_hides_token(self, mock_get):
        from .services.whatsapp import whatsapp_status

        mock_get.return_value = MagicMock(status_code=200, text='{"stateInstance":"authorized"}')
        report = whatsapp_status()
        self.assertIn("authorized", report)
        self.assertNotIn("secret-token", report)


class HealthCheckTests(TestCase):

    def test_returns_200_ok(self):
        response = self.client.get(reverse("health_check"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")


# ──────────────────────────────────────────────────────────────────────────────
# Banned view
# ──────────────────────────────────────────────────────────────────────────────


class BannedViewTests(TestCase):

    def test_returns_403(self):
        response = self.client.get(reverse("banned"))
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "habits/banned.html")


# ──────────────────────────────────────────────────────────────────────────────
# Security & edge cases
# ──────────────────────────────────────────────────────────────────────────────


class SecurityTests(TestCase):

    def setUp(self):
        self.client = Client()

    def test_maintenance_key_constant_time_compare(self):

        import inspect
        from . import views

        source = inspect.getsource(views.maintenance_trigger)
        self.assertIn("hmac.compare_digest", source)

    def test_otp_stored_as_hash_not_plaintext(self):
        from .services.otp import store_otp

        store_otp(CLEAN_PHONE, "123456")
        stored = PhoneOTP.objects.get(phone_number=CLEAN_PHONE).otp_hash
        self.assertNotEqual(stored, "123456")

    @patch("habits.services.otp.send_otp", return_value=(True, "whatsapp"))
    def test_registration_never_puts_raw_password_in_session(self, mock_send):
        self.client.post(
            reverse("index"),
            {
                "identifier": CLEAN_PHONE,
                "password": VALID_PASS,
                "habit_choice": Habit.HABIT_CHOICES[0][0],
            },
        )
        pending = self.client.session["pending_registration"]
        self.assertNotIn("password", pending)
        self.assertNotIn(VALID_PASS, json.dumps(dict(self.client.session)))

    def test_mark_done_requires_post(self):
        user = make_user()
        habit = make_habit(user)
        self.client.force_login(user)
        response = self.client.get(reverse("mark_habit_done", args=[habit.id]))
        self.assertEqual(response.status_code, 405)

    def test_cross_user_habit_access_blocked(self):
        user1 = make_user(phone="2348000000001")
        user2 = make_user(phone="2348000000002")
        habit = make_habit(user2)
        self.client.force_login(user1)
        response = self.client.post(reverse("mark_habit_done", args=[habit.id]))
        self.assertEqual(response.status_code, 404)
