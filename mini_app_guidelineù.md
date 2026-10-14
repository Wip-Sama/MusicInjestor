# Telegram Mini Apps (TMA) — Master Architecture, Security & UI/UX Specification
> **System Context / LLM Directive:** Use this specification as the authoritative architectural, security, and UI/UX baseline whenever designing, generating, refactoring, or reviewing code for a Telegram Mini App (TMA). Every component, API route, and user flow must adhere strictly to the invariants defined below.

---

## 1. Core Paradigm: The "App-in-App" Execution Model

Telegram Mini Apps are **not** standalone mobile websites wrapped in an `iframe`. They operate as native extensions of the Telegram client container across iOS, Android, macOS, Windows, Linux, and Web.

### 1.1 The Three-Tier Synchronization Architecture
1. **Tier 1: Native Client Container (Telegram Host OS)**
   - Controls window chrome (Top Header, Close/Back buttons, Bottom Action Bar with `MainButton` & `SecondaryButton`, native popups, hardware sensors, and viewport geometry).
2. **Tier 2: WebView Runtime & JS Bridge (`@telegram-apps/sdk`)**
   - Communicates bidirectionally with the host container via `window.Telegram.WebApp` or the modular `@telegram-apps/sdk` and `@telegram-apps/bridge` packages.
   - Dispatches lifecycle events, listens to real-time theme/viewport changes, and exposes cryptographically signed launch payloads (`initData`).
3. **Tier 3: Zero-Trust Backend & Bot API Server (HTTPS)**
   - Validates `initData` cryptographic signatures (`HMAC-SHA256` or `Ed25519`), persists state, generates payment invoices (`XTR`), and triggers asynchronous Bot API messages.

### 1.2 Strict Operational & Performance Boundaries
| Operational Metric / Constraint | Mandatory Platform Threshold | Architectural & UX Engineering Implication |
| :--- | :--- | :--- |
| **Hard WebView Load Timeout** | `< 10.0 seconds` | Telegram forcibly terminates and closes the WebView if initialization exceeds 10s. Use aggressive code-splitting and edge CDN caching. |
| **Largest Contentful Paint (LCP)** | `<= 2.5 seconds` | Render the primary visual shell rapidly using lightweight skeleton screens and Service Worker caching. |
| **Interaction to Next Paint (INP)** | `<= 200 milliseconds` | Keep input handlers non-blocking; pair state mutations with immediate native haptic feedback. |
| **Animation Frame Rate** | `60 fps` | Restrict animations to GPU-accelerated CSS properties (`transform`, `opacity`). |
| **Transport Security** | `Strict HTTPS (TLS 1.2+)` | Mandatory for both production and local mobile device testing (use `ngrok` or Cloudflare Tunnels; self-signed certs are rejected by mobile WebViews). |
| **Initialization Signal** | Immediate `miniApp.ready()` | Invoke `Telegram.WebApp.ready()` (`miniApp.ready()`) as early as possible on mount to dismiss the native loading spinner. |

---

## 2. Contextual Triggering: When to Use a Mini App vs. Conversational Bot

Do not force simple single-turn interactions into a WebView, and do not force multi-dimensional state into chat bubbles.

### 2.1 Decision Matrix: Conversational Bot vs. Mini App WebView
| Interaction Dimension | Conversational Bot Thread (Text / Inline Keyboards) | Telegram Mini App (WebView Interface) |
| :--- | :--- | :--- |
| **Information Density** | Low: 1 to 3 data points per message bubble. | High: Dashboards, multi-item catalogs, learning roadmaps, leaderboards. |
| **Input Complexity** | Linear: Single text reply or 1-tap button selection. | Multi-dimensional: Date/time pickers, searchable dropdowns, multi-field forms, sliders. |
| **State Mutation** | Stateless or sequential wizard via chat history. | Stateful client-side manipulation (shopping carts, interactive canvases, real-time games). |
| **Hardware & Sensor Access** | Static location or contact attachment only. | Full access to `BiometricManager`, `Gyroscope`, `Accelerometer`, live `LocationManager`, QR scanner. |
| **Visual Customization** | Standard text formatting, stickers, and media attachments. | Full DOM/WebGL/Canvas rendering synced with dynamic Telegram theme palettes. |

### 2.2 The Eight Official Launch Entry Points & Data Flow Rules
**CRITICAL ARCHITECTURAL RULE:** `sendData()` and `initData` are **mutually exclusive**. Calling `Telegram.WebApp.sendData()` inside an app launched via an Inline Button, Menu Button, or Direct Link **fails silently**.

| Entry Point Mechanism | `initData` Available? | `sendData()` Supported? | `query_id` (`answerWebAppQuery`) | Primary UX / Product Use Case |
| :--- | :--- | :--- | :--- | :--- |
| **1. Reply Keyboard Button** (`KeyboardButton.web_app`) | **NO** | **YES** (Max 4,096 bytes, 1-shot, closes app) | NO | Serverless data collection (custom date pickers, quick filters) returning a service message to the bot. |
| **2. Inline Button** (`InlineKeyboardButton.web_app`) | **YES** | **NO** (Use HTTPS API) | **YES** | Contextual launch from rich notification messages or transactional prompts. |
| **3. Bot Menu Button** (`setChatMenuButton`) | **YES** | **NO** (Use HTTPS API) | **YES** | Persistent primary entry point anchored next to the chat input field. |
| **4. Main Mini App** (Profile Launch / App Store) | **YES** | **NO** (Use HTTPS API) | NO | Flagship standalone app launch from bot profile or Telegram Mini App Store. |
| **5. Direct Link** (`t.me/bot/app?startapp=...`) | **YES** | **NO** (Use HTTPS API) | NO | Viral referral links, deep-linking into specific views, external marketing attribution. |
| **6. Attachment Menu** (`AttachmentMenuBot`) | **YES** | **NO** (Use HTTPS API) | **YES** | Launching directly from the paperclip attachment menu inside personal/group chats. |
| **7. Inline Mode** (`InlineQueryResultsButton`) | **YES** | **NO** (Use `switchInlineQuery`) | NO | Creating rich content in a WebView and returning to inline mode (`@bot query`) to post in any chat. |
| **8. Chat Join Request** (Bot API 10.1+) | **YES** (`chat_join_request_query_id`) | **NO** (Use HTTPS API) | NO | Custom CAPTCHA, Web3 token-gating, or payment verification before admitting a user to a channel/group. |

### 2.3 Deep-Linking & Query Parameter Sanitization
- Telegram **strips all standard URL query parameters** (`?foo=1&bar=2`) when opening a Direct Link.
- Only the single parameter `?startapp=<value>` is preserved and injected into `initDataUnsafe.start_param` (and signed inside `initData`).
- **Rule:** Serialize complex routing paths, referral codes, and UTM parameters into a single URL-safe string (e.g., `base64url` or delimiter-separated `route_product_123__ref_999`) inside `startapp`.

---

## 3. System Architecture, SDK Ecosystem & State Persistence

### 3.1 Standardized Package Stack (`@telegram-apps`)
Always use the official modular `@telegram-apps` packages. Never use deprecated legacy wrappers (`@twa-dev/sdk`).
- `@telegram-apps/sdk`: Core typed client SDK (`backButton`, `mainButton`, `secondaryButton`, `miniApp`, `viewport`, `biometry`, `popup`).
- `@telegram-apps/sdk-react` (or `-vue`, `-solid`, `-svelte`): Reactive hooks (`useSignal`, `useRawInitData`, `useLaunchParams`).
- `@telegram-apps/telegram-ui`: Native-styled React component system (`AppRoot`, `Cell`, `Section`, `Modal`, `Placeholder`) that automatically adapts to iOS and Android platform norms.
- `@telegram-apps/init-data-node`: Backend cryptographic validator for Node.js.

### 3.2 SSR & Hydration Guard Pattern (Next.js / Remix / Nuxt)
`window.Telegram` does not exist on the server. Importing or calling SDK methods during SSR causes hydration crashes. Isolate SDK initialization inside a client-only boundary imported with `ssr: false`:

```tsx
// components/TelegramClientBootstrap.tsx
'use client';

import { useEffect } from 'react';
import {
  init,
  backButton,
  miniApp,
  viewport,
  closingBehavior,
  swipeBehavior,
} from '@telegram-apps/sdk-react';

export function TelegramClientBootstrap({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    try {
      init();

      if (miniApp.mount.isAvailable()) {
        miniApp.mount();
        miniApp.ready();
      }

      if (viewport.mount.isAvailable()) {
        viewport.mount().then(() => {
          if (viewport.expand.isAvailable()) viewport.expand();
        });
      }

      if (backButton.mount.isAvailable()) backButton.mount();

      if (closingBehavior.mount.isAvailable()) {
        closingBehavior.mount();
        closingBehavior.enableConfirmation();
      }

      if (swipeBehavior.mount.isAvailable()) {
        swipeBehavior.mount();
        swipeBehavior.disableVertical();
      }
    } catch (err) {
      console.error('Telegram SDK initialization skipped outside native container:', err);
    }
  }, []);

  return <>{children}</>;
}
```

### 3.3 Resilient `initData` Capture Pipeline
Client-side routers frequently strip URL hash fragments (`#tgWebAppData=...`) on route transitions or reloads.
1. Extract `tgWebAppData` immediately prior to router initialization and store it in `sessionStorage`.
2. Resolve authentication headers via a cascading lookup:
   `sessionStorage.getItem('tg_init_data') || Telegram.WebApp.initData || extractFromUrlHash()`
3. Attach the raw string to every backend request via `X-Telegram-Init-Data` or `Authorization: tma <rawInitData>`.

### 3.4 Multi-Tier Storage Stratification
| Storage API | Capacity & Scope | Persistence Guarantee | Optimal Architectural Use Case |
| :--- | :--- | :--- | :--- |
| `sessionStorage` | ~5 MB (Session local) | Cleared when WebView closes. | Preserving raw `tgWebAppData` across SPA routes and transient wizard steps. |
| `localStorage` / `IndexedDB` | ~5–50 MB (Device local) | Vulnerable to OS cache eviction. | Caching non-critical UI state, API responses, and offline Service Worker assets. |
| `Telegram.WebApp.CloudStorage` | Up to **1,024 keys** per user | Synced across iOS, Android, and Desktop via Telegram Cloud. | Cross-device preferences, onboarding flags, favorites, and lightweight game state. |
| `Telegram.WebApp.SecureStorage` | Hardware-backed encryption | Persisted in device Keychain / Keystore. | Storing sensitive cryptographic keys, refresh tokens, or biometric-gated secrets. |

---

## 4. Zero-Trust Security & Cryptographic Authentication

**ABSOLUTE SECURITY INVARIANT:** Never trust `Telegram.WebApp.initDataUnsafe` on the backend. Every authenticated API endpoint must verify the cryptographic signature of the raw `initData` string and enforce an `auth_date` expiration check.

### 4.1 String Canonicalization Rule (Preventing Double URL-Decode Bugs)
The `user` field inside `initData` is a URL-encoded JSON string.
- Perform **exactly one** URL-decoding pass (`urllib.parse.parse_qsl` in Python or `new URLSearchParams()` in JS) on the raw `initData` string before sorting keys.
- **Never** parse and re-serialize the `user` JSON object before constructing `data_check_string`, as key ordering or whitespace differences will invalidate the signature.

### 4.2 Path A: First-Party Bot Server Validation (`HMAC-SHA256`)
1. Parse `initData` query string into key-value pairs.
2. Extract and remove `hash`. (Retain `signature` if present).
3. Verify `now() - int(auth_date) <= max_age_seconds` (typically 3,600 to 86,400 seconds).
4. Sort remaining keys lexicographically (`a` to `z`) and join as `key=value` separated by `\n` (`0x0A`).
5. Compute `secret_key = HMAC_SHA256(key="WebAppData", msg=BOT_TOKEN)`.
6. Compute `calculated_hash = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))`.
7. Compare `calculated_hash` with `received_hash` using a constant-time comparison function.

```python
# Reference Python Implementation: First-Party HMAC-SHA256 Validator
import hmac
import hashlib
import json
import time
from urllib.parse import parse_qsl


def validate_telegram_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict:
    if not init_data:
        raise ValueError("Missing initData payload")

    parsed_pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed_pairs.pop("hash", None)
    if not received_hash:
        raise ValueError("Missing cryptographic hash in initData")

    auth_date_str = parsed_pairs.get("auth_date")
    if not auth_date_str or (int(time.time()) - int(auth_date_str)) > max_age_seconds:
        raise ValueError("Expired initData session")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_pairs.items()))

    secret_key = hmac.new(key=b"WebAppData", msg=bot_token.encode("utf-8"), digestmod=hashlib.sha256).digest()

    calculated_hash = hmac.new(
        key=secret_key, msg=data_check_string.encode("utf-8"), digestmod=hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise ValueError("Invalid initData HMAC signature")

    if "user" in parsed_pairs:
        parsed_pairs["user"] = json.loads(parsed_pairs["user"])

    return parsed_pairs
```

### 4.3 Universal Edge Runtime Validator (Web Crypto API — Zero Node Dependencies)
Use this implementation in Cloudflare Workers, Vercel Edge, Deno, or Convex where Node's `Buffer` is unavailable:

```typescript
export async function verifyTelegramInitDataEdge(
  rawInitData: string,
  botToken: string,
  maxAgeSeconds = 86400
): Promise<boolean> {
  const params = new URLSearchParams(rawInitData);
  const receivedHash = params.get('hash');
  const authDate = Number(params.get('auth_date'));

  if (!receivedHash || !authDate) return false;
  if (Math.floor(Date.now() / 1000) - authDate > maxAgeSeconds) return false;

  params.delete('hash');

  const dataCheckString = Array.from(params.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}=${v}`)
    .join('\n');

  const encoder = new TextEncoder();

  const webAppDataKey = await crypto.subtle.importKey(
    'raw',
    encoder.encode('WebAppData'),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );

  const secretKeyBuffer = await crypto.subtle.sign(
    'HMAC',
    webAppDataKey,
    encoder.encode(botToken)
  );

  const hmacKey = await crypto.subtle.importKey(
    'raw',
    secretKeyBuffer,
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  );

  const signatureBuffer = await crypto.subtle.sign(
    'HMAC',
    hmacKey,
    encoder.encode(dataCheckString)
  );

  const calculatedHash = Array.from(new Uint8Array(signatureBuffer))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');

  return calculatedHash === receivedHash;
}
```

### 4.4 Path B: Third-Party Tokenless Validation (`Ed25519`)
When verifying `initData` in a microservice or third-party backend **without** access to `BOT_TOKEN`:
1. Exclude **both** `hash` and `signature` from the parsed parameters.
2. Construct `data_check_string` by prepending `<bot_id>:WebAppData\n` followed by the sorted `key=value` pairs joined by `\n`.
3. Decode the `base64url`-encoded `signature` parameter and verify it using `Ed25519` against Telegram's official public key:
   - **Production Public Key:** `e7bf03a2fa4602af4580703d88dda5bb59f32ed8b02a56c187fe7d34caed242d`
   - **Test Environment Public Key:** `40055058a4ee38156a06562e52eece92a771bcd8346a8c4615cb7376eddf72ec`

---

## 5. Native-First UI/UX & Human Interface Guidelines

### 5.1 Dynamic Theming & CSS Custom Properties
Never hardcode static light/dark hex colors. Bind all surfaces and typography to Telegram's reactive CSS variables and call `setHeaderColor()`, `setBackgroundColor()`, and `setBottomBarColor()` on mount and `themeChanged` events:

| CSS Variable | `themeParams` Key | UI Surface / Role |
| :--- | :--- | :--- |
| `var(--tg-theme-bg-color)` | `bg_color` | Primary background surface for single-layer views. |
| `var(--tg-theme-secondary-bg-color)` | `secondary_bg_color` | Grouped list background (`iOS Inset Grouped` style behind cards). |
| `var(--tg-theme-section-bg-color)` | `section_bg_color` | Elevated card, cell, or grouped section background. |
| `var(--tg-theme-header-bg-color)` | `header_bg_color` | Top bar background; sync via `setHeaderColor()`. |
| `var(--tg-theme-bottom-bar-bg-color)` | `bottom_bar_bg_color` | Native bottom bar background behind `MainButton` / `SecondaryButton`. |
| `var(--tg-theme-text-color)` | `text_color` | Primary body copy and headlines (maintain `>= 4.5:1` WCAG 2.2 contrast). |
| `var(--tg-theme-subtitle-text-color)` | `subtitle_text_color` | Secondary descriptions, metadata, and captions. |
| `var(--tg-theme-section-header-text-color)` | `section_header_text_color` | Group headers above list sections. |
| `var(--tg-theme-accent-text-color)` | `accent_text_color` | Interactive inline links, active tab indicators, highlights. |
| `var(--tg-theme-button-color)` | `button_color` | Primary button fill color. |
| `var(--tg-theme-button-text-color)` | `button_text_color` | Primary button label color. |
| `var(--tg-theme-destructive-text-color)` | `destructive_text_color` | Delete actions, error states, and irreversible warnings. |
| `var(--tg-theme-section-separator-color)` | `section_separator_color` | Hairline `1px` dividers between cells and sections. |

### 5.2 Viewport Geometry, Fullscreen Mode & Dual Safe Area Insets (Bot API 8.0+)
- **Never use `100vh`:** Mobile keyboards and collapsing sheets break `100vh`. Use `var(--tg-viewport-height)` or `Telegram.WebApp.viewportHeight` and listen to `viewportChanged` (waiting for `isStateStable === true` before scrolling focused inputs into view).
- **Dual Safe Areas in Fullscreen Mode (`requestFullscreen()`):**
  - `safeAreaInset` (`--tg-safe-area-inset-top/bottom/left/right`): Physical OS hardware cutouts (iOS Dynamic Island, camera notch, home indicator bar).
  - `contentSafeAreaInset` (`--tg-content-safe-area-inset-top/bottom/left/right`): Inner margin required to clear Telegram's floating fullscreen header controls (Close, Minimize, Options pills).
  - **Total Top Padding Formula:** `calc(var(--tg-safe-area-inset-top, 0px) + var(--tg-content-safe-area-inset-top, 0px))`

### 5.3 Gesture Conflict Resolution & Native Chrome Delegation
1. **Prevent Accidental Swipe Closure:** Always call `Telegram.WebApp.disableVerticalSwipes()` (`swipeBehavior.disableVertical()`) on scrollable apps, maps, and games so downward scrolling does not collapse the Mini App drawer.
2. **Prevent Unsaved Data Loss:** Call `Telegram.WebApp.enableClosingConfirmation()` whenever a user has items in a cart, unsaved form inputs, or an active session.
3. **Delegate Primary CTAs:** Do not build sticky HTML bottom buttons. Use `Telegram.WebApp.MainButton` and `SecondaryButton` (Bot API 7.10+). Always show the loading spinner (`MainButton.showProgress()`) and disable the button during in-flight network requests.
4. **Delegate Navigation & Alerts:** Bind SPA route history to `Telegram.WebApp.BackButton` (hide on root route, show on child routes). Use `showPopup()`, `showAlert()`, and `showConfirm()` instead of DOM modal overlays for system alerts.
5. **Tactile Haptic Feedback (`HapticFeedback`):**
   - `selectionChanged()`: Tab switches, carousel snaps, picker wheel ticks.
   - `impactOccurred('light' | 'medium' | 'heavy' | 'rigid' | 'soft')`: Button presses, toggle switches, physics collisions.
   - `notificationOccurred('success' | 'warning' | 'error')`: Checkout completion, validation errors, destructive action confirmations.
6. **"Metro Mode" Offline Resilience:** Register a Service Worker to cache the app shell and static assets. When offline, render cached data from `IndexedDB`/`CloudStorage` with a native-styled "Reconnecting..." banner rather than a browser error screen. Use `rem`/`em` units to respect OS Dynamic Type scaling.

---

## 6. Hardware APIs, Viral Mechanics & Monetization

### 6.1 Device Hardware & Sensor Managers
- **`BiometricManager` (Bot API 7.2+):** Gate sensitive actions, key exports, or transactions behind FaceID / TouchID (`init()` -> `requestAccess()` -> `authenticate()`).
- **`LocationManager` (Bot API 8.0+):** Request live coordinates for delivery apps or geo-games (`init()` -> `getLocation()`).
- **Motion Sensors (Bot API 8.0+):** `Accelerometer`, `Gyroscope`, and `DeviceOrientation` (`start({ refresh_rate })`, `stop()`).
- **QR & Clipboard:** Use `showScanQrPopup()` for scanning wallet addresses or promo codes natively without WebRTC camera permission prompts.

### 6.2 Viral Loops & Retention Mechanics
- **Direct Chat Sharing (`shareMessage` - Bot API 8.0+):** Backend calls Bot API `savePreparedInlineMessage` -> returns `prepared_message_id` -> Frontend calls `Telegram.WebApp.shareMessage(id)` to let users forward rich interactive cards to friends or groups.
- **Telegram Stories (`shareToStory` - Bot API 7.8+):** Publish generated achievement cards or referral stickers directly to Stories with a deep-link widget.
- **Home Screen Shortcuts (`addToHomeScreen` - Bot API 8.0+):** Check status via `checkHomeScreenStatus()` and prompt engaged users to install a 1-tap icon on their phone's OS home screen.

### 6.3 Telegram Stars (`XTR`) Payment Lifecycle
All digital goods and subscriptions must use **Telegram Stars (`XTR`)**.
1. **Backend Invoice Creation:** Call Bot API `createInvoiceLink` with `currency: "XTR"` and `provider_token: ""` (empty string).
2. **Frontend Payment Sheet:** Call `Telegram.WebApp.openInvoice(invoiceUrl, callback)`.
3. **Backend Webhook Fulfillment:** Handle `pre_checkout_query` within 10 seconds (`answerPreCheckoutQuery`) and grant the item **only** upon receiving the verified `successful_payment` webhook update on the backend.
4. **Desktop Top-Up Edge Case:** When a user has insufficient Stars and clicks "Pay" to open the "Buy Stars" top-up modal, Telegram Desktop immediately fires the `openInvoice` callback with `status: "failed"`. Do **not** show a hard error screen on `"failed"`; poll or listen via WebSocket for the backend `successful_payment` event while the user completes their Stars top-up.

### 6.4 Web3 & TON Connect Invariants (`@tonconnect/ui-react`)
- **`tonconnect-manifest.json`:** Must be publicly accessible over HTTPS with permissive CORS headers (`Access-Control-Allow-Origin: *`) and not blocked by Cloudflare bot challenges.
- **Manifest `iconUrl`:** Must be a **180x180 px PNG or ICO** file. **SVG icons are strictly rejected** by wallets (`MANIFEST_CONTENT_ERROR`).
- **Return Redirect:** Always set `actionsConfiguration={{ twaReturnUrl: 'https://t.me/<BotUsername>/<AppName>' }}` on `<TonConnectUIProvider>` so external wallets redirect back into Telegram after signing.
- **Address & Unit Formatting:** Format destination addresses in user-friendly `base64url` (`EQ...` or `UQ...`, never raw `0:...` hex) and express amounts in nanotons (`TON * 10^9`) as strings.

---

## 7. Master Verification Checklist for Code Generation & Review

Before finalizing any Telegram Mini App code, verify compliance against these 10 rules:
- [ ] **1. Official Modular SDK:** Uses `@telegram-apps/sdk` and `@telegram-apps/telegram-ui` (no legacy `@twa-dev/sdk`).
- [ ] **2. SSR Guarded:** All SDK imports and `window.Telegram` calls are isolated inside `'use client'` boundaries with `ssr: false`.
- [ ] **3. Fast Boot Lifecycle:** Calls `miniApp.ready()` immediately on mount; bundle loads well under the 10-second hard cutoff (`LCP <= 2.5s`).
- [ ] **4. Zero-Trust Auth:** Raw `initData` is preserved in `sessionStorage` and verified on every backend request via `HMAC-SHA256` (or `Ed25519`) + `auth_date` freshness check.
- [ ] **5. Single URL-Decode Pass:** Backend performs exactly one URL-decode pass on `initData` before sorting keys lexicographically with `\n`.
- [ ] **6. Launch Flow Compatibility:** Uses `sendData()` **only** for `KeyboardButton` launches; uses HTTPS requests / `answerWebAppQuery` for Inline, Menu, and Direct Link launches.
- [ ] **7. Sanitized Deep-Linking:** Encodes all deep-link state into the single `?startapp=` parameter (`initDataUnsafe.start_param`).
- [ ] **8. Safe Areas & Swipe Lock:** Binds layout height to `viewportHeight`, applies both `safeAreaInset` and `contentSafeAreaInset` in fullscreen mode, and calls `disableVerticalSwipes()`.
- [ ] **9. Native Chrome & Haptics:** Delegates primary CTAs to `MainButton`/`SecondaryButton`, navigation to `BackButton`, feedback to `HapticFeedback`, and colors to `var(--tg-theme-*)`.
- [ ] **10. Stars & TON Compliance:** Uses `currency: "XTR"` + `provider_token: ""` with webhook fulfillment for Stars; serves a CORS-enabled `tonconnect-manifest.json` with a 180x180 PNG icon and `twaReturnUrl`.