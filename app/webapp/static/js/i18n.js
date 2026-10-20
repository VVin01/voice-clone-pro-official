/**
 * i18n.js - Comprehensive Localization Engine for Studio Pro Mini App
 * Supports pure Burmese (my) and pure English (en) without hardcoded strings.
 */

const translations = {
    my: {
        // App Top Bar
        brand_title: "Studio Pro",
        brand_subtitle: "AI အသံနှင့် Recap ဖန်တီးမှု စနစ်",
        vip_free: "FREE အဖွဲ့ဝင်",
        vip_purchased: "👑 VIP အသင်းဝင်",
        unlimited: "အကန့်အသတ်မဲ့",

        // Bottom Navigation Tabs
        tab_voice: "Voice Studio",
        tab_recap: "Recap Maker",
        tab_downloader: "Downloader",
        tab_vip: "VIP Wallet",
        tab_admin: "Admin Console",

        // Tab 1: Voice Clone Studio
        voice_title: "🎙️ AI Voice Clone Studio",
        voice_subtitle: "သဘာဝကျသော မြန်မာ AI အသံသွင်းခြင်း နှင့် အသံတုပခြင်း",
        engine_label: "အသုံးပြုမည့် AI Engine",
        engine_voxcpm_title: "☁️ VoxCPM (Cloud AI)",
        engine_voxcpm_desc: "Zero-shot သဘာဝအသံ • Cloud တွင် တိုက်ရိုက်လုပ်ဆောင်သည်",
        engine_applio_title: "💻 Applio RVC (Local GPU)",
        engine_applio_desc: "Laptop Worker တွင် Train ထားသော မော်ဒယ်များ",

        persona_mode_title: "အသံရွေးချယ်မှု စတိုင်လ်",
        btn_mode_curated: "🌟 Curated Voices (၆ မျိုး)",
        btn_mode_custom: "🎙️ Custom Voice Clone",

        curated_section_title: "ကြိုတင်သတ်မှတ်ထားသော Gemini အသံများ",
        custom_ref_title: "🎙️ Reference Voice (အသံတူ Clone ရန် အသံနမူနာ)",
        custom_ref_optional: "ရွေးချယ်ခွင့် (Optional)",
        custom_ref_desc: "မိမိပြောလိုသော အသံပိုင်ရှင်၏ အသံဖိုင် (၃-၃၀ စက္ကန့် WAV/MP3) တင်သွင်းနိုင်ပါသည်။ မတင်ပါက Default Burmese AI Voice ဖြင့် ပြောပါမည်။",
        ref_file_btn: "📁 Reference Audio ဖိုင် ရွေးချယ်မည်",
        btn_clear_file: "✕ ဖျက်မည်",

        model_picker_label: "ရွေးချယ်ထားသော အသံမော်ဒယ်",
        applio_status_active: "🟢 Local Worker အဆင်သင့်ဖြစ်ပါသည်",
        applio_status_waiting: "🟡 Laptop Worker စောင့်ဆိုင်းနေပါသည် (Queue)",

        text_input_label: "အသံသွင်းလိုသော စာသား (Text)",
        text_placeholder: "မြန်မာစာသားများကို ဤနေရာတွင် ရိုက်ထည့်ပါ သို့မဟုတ် ကူးထည့်ပါ...",
        sample_greeting: "💡 မင်္ဂလာပါ",
        sample_tech: "💡 AI နည်းပညာ",
        btn_clear_text: "🗑️ ရှင်းမည်",

        speed_label: "အမြန်နှုန်း (Speed):",
        speed_slow: "0.8x နှေး",
        speed_normal: "1.0x ပုံမှန်",
        speed_fast: "1.2x မြန်",

        btn_generate: "🚀 အသံ စတင်ထုတ်ယူမည်",
        preparing_msg: "ပြင်ဆင်နေပါသည်...",
        calculating_eta: "ခန့်မှန်းစောင့်ဆိုင်းချိန် တွက်ချက်နေပါသည်...",
        result_title: "🎉 အသံထုတ်ယူမှု ပြီးစီးပါပြီ",
        btn_download_wav: "📥 Download WAV",

        // Tab 2: Recap Maker
        recap_title: "🎬 AI Movie Recap Maker",
        recap_subtitle: "English ဗီဒီယိုများကို မြန်မာအသံ၊ စာတန်းထိုး နှင့် Blur ပါဝင်သော Recap ဖန်တီးပါ",
        recap_url_label: "YouTube သို့မဟုတ် TikTok Video Link",
        recap_url_placeholder: "https://youtu.be/... သို့မဟုတ် TikTok link...",
        btn_analyze: "စစ်ဆေးမည်",
        preview_label: "🖼️ Real-time Preview မျက်နှာပြင်",
        preview_hint: "Slider များကို ရွှေ့၍ နေရာညှိပါ",
        blur_box_label: "မူရင်းစာတန်းဖျက် Blur Box",
        sub_sample_text: "\"အခုတော့ ကျွန်တော်တို့ရဲ့ အစီအစဉ် စတင်ပါပြီ...\"",
        logo_label: "⭐ LOGO",
        slider_blur_y: "🔲 မူရင်းစာတန်းဖျက် Blur အနိမ့်အမြင့် (Y):",
        slider_blur_h: "↕️ Blur Box အထူ/အမြင့် (H):",
        slider_sub_y: "📝 မြန်မာစာတန်းထိုး နေရာ (Sub Y):",
        sub_style_label: "စာတန်းထိုး စတိုင်လ်:",
        recap_voice_label: "Recap အတွက် အသုံးပြုမည့် အသံ (Dub Voice)",
        btn_start_recap: "⚡ 1-Click Pro Recap ဖန်တီးမည်",

        // Tab 3: Downloader
        dl_title: "📥 Media Downloader",
        dl_subtitle: "YouTube နှင့် TikTok မှ Video (MP4)၊ Audio (MP3) နှင့် Subtitle (SRT) ဒေါင်းလုဒ် ရယူပါ",
        dl_url_label: "Video သို့မဟုတ် Music Link ကူးထည့်ပါ",
        dl_url_placeholder: "YouTube, TikTok, Shorts URL...",
        btn_search_dl: "🔍 ဒေါင်းလုဒ် ရွေးချယ်မှုများ ရှာဖွေမည်",
        fmt_video_title: "Full HD Video",
        fmt_video_desc: "MP4 (H.264 / AAC)",
        fmt_audio_title: "High-Quality Audio",
        fmt_audio_desc: "MP3 (192kbps)",
        fmt_sub_title: "Subtitle File",
        fmt_sub_desc: "SRT (Auto-captions)",

        // Tab 4: VIP Wallet
        vip_title: "👑 VIP အသင်းဝင် & Balance",
        vip_subtitle: "အကန့်အသတ်မဲ့ အသံသွင်းခွင့်နှင့် Pro Recap သီးသန့် အခွင့်အရေးများ",
        stat_current_tier: "လက်ရှိ အဆင့်အတန်း",
        stat_balance_min: "လက်ကျန် မိနစ် (Minutes)",
        plan_1m_name: "1 Month Unlimited VIP",
        plan_1m_price: "15,000",
        plan_1m_badge: "လူကြိုက်အများဆုံး 🔥",
        plan_1m_f1: "✓ Full HD Movie Recap ဖန်တီးခွင့် မကန့်သတ်",
        plan_1m_f2: "✓ VoxCPM Cloud AI & Applio အသံသွင်းခွင့်",
        plan_1m_f3: "✓ မူရင်းစာတန်းဖျက် Smart Blur & Logo Watermark",
        plan_1m_f4: "✓ အမြင့်ဆုံး Speed 192k MP3 & Subtitles",

        plan_3m_name: "3 Months Studio Pro",
        plan_3m_price: "35,000",
        plan_3m_badge: "တန်ဖိုးအရှိဆုံး 💎",
        plan_3m_f1: "✓ ၃ လ သက်တမ်း အကန့်အသတ်မဲ့ အသုံးပြုခွင့်",
        plan_3m_f2: "✓ မိမိကိုယ်ပိုင် Clone Voice Slot (၁) ခု အခမဲ့ Train ပေးခြင်း",
        plan_3m_f3: "✓ Priority High-Speed Queue စနစ်",
        plan_3m_f4: "✓ 24/7 VIP Direct Support",

        btn_buy_1m: "💎 ယခု ဝယ်ယူမည်",
        btn_buy_3m: "👑 ယခု ဝယ်ယူမည်",
        payment_info_title: "💳 ငွေပေးချေမှု အချက်အလက်များ",
        copy_btn: "📋 ကူးမည်",
        payment_notice: "⚠️ ငွေလွှဲပြီးပါက ငွေလွှဲပြေစာ Screenshot ကို အောက်တွင် တင်သွင်းပေးပါ သို့မဟုတ် @VoiceCloneProAdmin သို့ ပို့ပေးပါရန်။",
        slip_upload_title: "📸 ငွေလွှဲပြေစာ (Screenshot) တင်သွင်းရန်",
        btn_submit_slip: "📤 ပြေစာ တင်သွင်းမည်",

        // Tab 5: Admin Console
        admin_title: "⚡ Admin Control Console",
        admin_subtitle: "စနစ်ကြီးကြပ်မှု၊ ငွေလွှဲပြေစာ စစ်ဆေးခြင်းနှင့် အသုံးပြုသူများ စီမံခန့်ခွဲမှု",
        admin_stat_users: "အသုံးပြုသူ စုစုပေါင်း",
        admin_stat_vip: "VIP အသင်းဝင်များ",
        admin_stat_slips: "စစ်ဆေးရန် ပြေစာများ",
        admin_stat_queue: "Queue ရှိ အလုပ်များ",
        admin_slips_header: "📋 စစ်ဆေးရန် စောင့်ဆိုင်းနေသော ငွေလွှဲပြေစာများ",
        admin_no_slips: "စစ်ဆေးရန် ပြေစာ မရှိသေးပါ",
        btn_approve: "✓ အတည်ပြုမည် (+VIP)",
        btn_reject: "✕ ပယ်ဖျက်မည်",
        admin_user_manage_header: "👤 အသုံးပြုသူ ရှာဖွေ ပြင်ဆင်ခြင်း",
        admin_user_id_ph: "Telegram ID (ဥပမာ: 1957002115)",
        btn_grant_vip: "👑 VIP သို့ တင်မည်",
        btn_add_minutes: "⏱️ +60 မိနစ် ထည့်မည်",
        admin_broadcast_header: "📢 Telegram Bot အားလုံးသို့ Broadcast စာပို့ရန်",
        admin_broadcast_ph: "အသုံးပြုသူ အားလုံးထံ ပေးပို့မည့် စာသား ရိုက်ထည့်ပါ...",
        btn_send_broadcast: "🚀 Broadcast ပို့မည်",

        // Notifications & Toasts
        toast_copied: "ကူးယူပြီးပါပြီခင်ဗျာ!",
        toast_empty_text: "ကျေးဇူးပြု၍ စာသား ရိုက်ထည့်ပေးပါခင်ဗျာ။",
        toast_empty_url: "ကျေးဇူးပြု၍ Video URL ထည့်သွင်းပေးပါခင်ဗျာ။",
        toast_success_slip: "ပြေစာ အောင်မြင်စွာ တင်သွင်းပြီးပါပြီ။",
    },

    en: {
        // App Top Bar
        brand_title: "Studio Pro",
        brand_subtitle: "AI Voice & Recap Studio",
        vip_free: "FREE MEMBER",
        vip_purchased: "👑 VIP PRO",
        unlimited: "Unlimited",

        // Bottom Navigation Tabs
        tab_voice: "Voice Studio",
        tab_recap: "Recap Maker",
        tab_downloader: "Downloader",
        tab_vip: "VIP Wallet",
        tab_admin: "Admin Console",

        // Tab 1: Voice Clone Studio
        voice_title: "🎙️ AI Voice Clone Studio",
        voice_subtitle: "Natural Myanmar Speech Synthesis & Voice Cloning",
        engine_label: "AI Synthesis Engine",
        engine_voxcpm_title: "☁️ VoxCPM (Cloud AI)",
        engine_voxcpm_desc: "Zero-shot human natural speech • Real-time cloud",
        engine_applio_title: "💻 Applio RVC (Local GPU)",
        engine_applio_desc: "Local trained models on laptop worker",

        persona_mode_title: "Voice Selection Mode",
        btn_mode_curated: "🌟 Curated Personas (6)",
        btn_mode_custom: "🎙️ Custom Voice Clone",

        curated_section_title: "Pre-tuned Gemini Voice Personas",
        custom_ref_title: "🎙️ Reference Voice (Clone Sample)",
        custom_ref_optional: "Optional",
        custom_ref_desc: "Upload target voice sample (3-30s WAV/MP3). If left empty, default Burmese AI voice will be used.",
        ref_file_btn: "📁 Choose Reference Audio File",
        btn_clear_file: "✕ Clear",

        model_picker_label: "Selected Voice Model",
        applio_status_active: "🟢 Local Worker Online",
        applio_status_waiting: "🟡 Waiting for Laptop Worker (Queued)",

        text_input_label: "Input Script Text",
        text_placeholder: "Type or paste Myanmar text here...",
        sample_greeting: "💡 Greetings",
        sample_tech: "💡 AI Technology",
        btn_clear_text: "🗑️ Clear",

        speed_label: "Speech Rate (Speed):",
        speed_slow: "0.8x Slow",
        speed_normal: "1.0x Normal",
        speed_fast: "1.2x Fast",

        btn_generate: "🚀 Synthesize Speech",
        preparing_msg: "Preparing synthesis...",
        calculating_eta: "Estimating queue duration...",
        result_title: "🎉 Voice Generated Successfully",
        btn_download_wav: "📥 Download WAV",

        // Tab 2: Recap Maker
        recap_title: "🎬 AI Movie Recap Maker",
        recap_subtitle: "Transform English videos into Myanmar dubbed recaps with smart sync & subtitles",
        recap_url_label: "YouTube or TikTok Video Link",
        recap_url_placeholder: "https://youtu.be/... or TikTok link...",
        btn_analyze: "Analyze Link",
        preview_label: "🖼️ Real-time Canvas Preview",
        preview_hint: "Adjust sliders to position overlays",
        blur_box_label: "Subtitle Deletion Blur Box",
        sub_sample_text: "\"Now our story officially begins...\"",
        logo_label: "⭐ LOGO",
        slider_blur_y: "🔲 Subtitle Blur Position (Y):",
        slider_blur_h: "↕️ Blur Box Height (H):",
        slider_sub_y: "📝 Subtitle Vertical Position (Sub Y):",
        sub_style_label: "Subtitle Typography Style:",
        recap_voice_label: "Narrator Dubbing Voice",
        btn_start_recap: "⚡ Generate 1-Click Pro Recap",

        // Tab 3: Downloader
        dl_title: "📥 Media Downloader",
        dl_subtitle: "Download high-speed MP4 Video, MP3 Audio, and SRT Subtitles from YouTube & TikTok",
        dl_url_label: "Paste Video or Audio Link",
        dl_url_placeholder: "YouTube, TikTok, Shorts URL...",
        btn_search_dl: "🔍 Fetch Download Formats",
        fmt_video_title: "Full HD Video",
        fmt_video_desc: "MP4 (H.264 / AAC)",
        fmt_audio_title: "High-Quality Audio",
        fmt_audio_desc: "MP3 (192kbps)",
        fmt_sub_title: "Subtitle File",
        fmt_sub_desc: "SRT (Auto-captions)",

        // Tab 4: VIP Wallet
        vip_title: "👑 VIP Membership & Wallet",
        vip_subtitle: "Unlimited voice cloning allowance & full movie recap access",
        stat_current_tier: "Current Membership Tier",
        stat_balance_min: "Remaining Minutes",
        plan_1m_name: "1 Month Unlimited VIP",
        plan_1m_price: "15,000",
        plan_1m_badge: "Most Popular 🔥",
        plan_1m_f1: "✓ Full HD Movie Recap creation unlimited",
        plan_1m_f2: "✓ VoxCPM Cloud AI & Applio Voice Cloning",
        plan_1m_f3: "✓ Anti-Copyright Smart Blur & Logo Watermark",
        plan_1m_f4: "✓ High-speed 192k MP3 & Subtitles",

        plan_3m_name: "3 Months Studio Pro",
        plan_3m_price: "35,000",
        plan_3m_badge: "Best Value 💎",
        plan_3m_f1: "✓ 3 Months unrestricted studio usage",
        plan_3m_f2: "✓ Free custom trained clone voice slot (1)",
        plan_3m_f3: "✓ Priority High-Speed Queue access",
        plan_3m_f4: "✓ 24/7 VIP Direct Support",

        btn_buy_1m: "💎 Upgrade Now",
        btn_buy_3m: "👑 Upgrade Now",
        payment_info_title: "💳 Payment Account Details",
        copy_btn: "📋 Copy",
        payment_notice: "⚠️ After payment, upload your transfer screenshot below or send to @VoiceCloneProAdmin for instant activation.",
        slip_upload_title: "📸 Upload Payment Screenshot",
        btn_submit_slip: "📤 Submit Receipt Slip",

        // Tab 5: Admin Console
        admin_title: "⚡ Admin Control Console",
        admin_subtitle: "Operations overview, payment verification, and user management",
        admin_stat_users: "Total Registered Users",
        admin_stat_vip: "VIP Members",
        admin_stat_slips: "Pending Slips",
        admin_stat_queue: "Queued Jobs",
        admin_slips_header: "📋 Payment Transfer Slips Awaiting Review",
        admin_no_slips: "No pending payment slips",
        btn_approve: "✓ Approve (+VIP)",
        btn_reject: "✕ Reject",
        admin_user_manage_header: "👤 User Tier & Minutes Management",
        admin_user_id_ph: "Telegram ID (e.g. 1957002115)",
        btn_grant_vip: "👑 Upgrade to VIP",
        btn_add_minutes: "⏱️ Add +60 Minutes",
        admin_broadcast_header: "📢 Broadcast Message to All Users",
        admin_broadcast_ph: "Type broadcast message to deliver to all users...",
        btn_send_broadcast: "🚀 Send Broadcast",

        // Notifications & Toasts
        toast_copied: "Copied to clipboard!",
        toast_empty_text: "Please enter some text to synthesize.",
        toast_empty_url: "Please enter a valid video URL.",
        toast_success_slip: "Payment slip submitted successfully.",
    }
};

let currentLang = localStorage.getItem("studio_lang") || "my";

function t(key) {
    const dict = translations[currentLang] || translations.my;
    return dict[key] || translations.en[key] || key;
}

function setLanguage(lang) {
    if (lang !== "my" && lang !== "en") lang = "my";
    currentLang = lang;
    localStorage.setItem("studio_lang", lang);

    // Update toggle button text
    const langBtn = document.getElementById("btn-lang-toggle");
    if (langBtn) {
        langBtn.innerText = lang === "my" ? "🇲🇲 မြန်မာ" : "🇬🇧 EN";
    }

    // Apply to all elements with data-i18n
    document.querySelectorAll("[data-i18n]").forEach(el => {
        const key = el.getAttribute("data-i18n");
        if (key && translations[lang] && translations[lang][key]) {
            el.innerText = translations[lang][key];
        }
    });

    // Apply placeholders
    document.querySelectorAll("[data-i18n-ph]").forEach(el => {
        const key = el.getAttribute("data-i18n-ph");
        if (key && translations[lang] && translations[lang][key]) {
            el.placeholder = translations[lang][key];
        }
    });

    // Notify backend if user is authenticated
    if (window.getAuthHeaders) {
        fetch("/api/user/language", {
            method: "POST",
            headers: window.getAuthHeaders(),
            body: JSON.stringify({ language: lang }),
        }).catch(() => {});
    }
}
