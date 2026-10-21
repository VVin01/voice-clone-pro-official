/**
 * i18n.js - Comprehensive Localization Engine for Studio Pro Mini App
 * Pure Burmese (my) and Pure English (en) without mixed or hardcoded strings.
 */

const translations = {
    my: {
        // App Top Bar & Brand
        brand_title: "VOICE CLONE PRO",
        brand_subtitle: "STUDIO • ULTRA SUITE",
        btn_tutorial: "လမ်းညွှန်",
        tutorial_banner_title: "အသုံးပြုနည်း လမ်းညွှန်ဗီဒီယို",
        tutorial_banner_desc: "Voice Cloning စတင်အသုံးပြုပုံ အဆင့်ဆင့် ဗီဒီယိုကို ကြည့်ရှုပါ",
        recap_tutorial_title: "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်",
        recap_tutorial_desc: "ရုပ်ရှင်ဇာတ်လမ်း သုံးသပ်ချက် ဗီဒီယို အလွယ်တကူ ဖန်တီးနည်းကို ကြည့်ရှုပါ",
        btn_watch: "ကြည့်ရှုရန် ▶️",
        vip_free: "FREE အဖွဲ့ဝင်",
        vip_purchased: "👑 VIP PRO",
        unlimited: "အကန့်အသတ်မဲ့",
        balance_voice_label: "🎙️ အသံသွင်းခွင့်:",
        balance_recap_label: "🎬 Recap Credits:",

        // Bottom Navigation Tabs (Burmese)
        tab_voice: "အသံဖန်တီး",
        tab_recap: "ဗီဒီယို Recap",
        tab_downloader: "ဒေါင်းလုဒ်",
        tab_vip: "VIP အစီအစဉ်",
        tab_admin: "အက်ဒမင်",

        // Tab 1: Voice Clone Studio
        voice_title: "🎙️ AI Voice Clone Studio",
        voice_subtitle: "သဘာဝကျသော မြန်မာ AI အသံသွင်းခြင်း နှင့် အဆင့်မြင့် Voice Cloning",
        engine_label: "အသုံးပြုမည့် AI Engine ရွေးချယ်ပါ",
        engine_voxcpm_title: "☁️ VoxCPM (Cloud AI)",
        engine_voxcpm_desc: "Zero-shot သဘာဝအသံ • Cloud တွင် တိုက်ရိုက်လုပ်ဆောင်သည်",
        engine_applio_title: "💻 Applio RVC (Local GPU)",
        engine_applio_desc: "Laptop Worker တွင် Train ထားသော မော်ဒယ်များ",

        persona_mode_title: "အသံရွေးချယ်မှု စတိုင်လ်",
        btn_mode_curated: "🌟 Curated Voices (၁၂ မျိုး)",
        btn_mode_custom: "🎙️ Custom Voice Clone",

        curated_section_title: "ကြိုတင်သတ်မှတ်ထားသော Gemini အသံများ (၁၂ မျိုး)",
        custom_ref_title: "🎙️ Reference Voice (အသံတူ Clone ရန် အသံနမူနာ)",
        custom_ref_optional: "ရွေးချယ်ခွင့် (Optional)",
        custom_ref_desc: "မိမိပြောလိုသော အသံပိုင်ရှင်၏ အသံဖိုင် (၃-၃၀ စက္ကန့် WAV/MP3) တင်သွင်းနိုင်ပါသည်။ မတင်ပါက Default Burmese AI Voice ဖြင့် ပြောပါမည်။",
        ref_file_btn: "📁 Reference Audio ဖိုင် ရွေးချယ်မည်",
        btn_clear_file: "✕ ဖျက်မည်",

        model_picker_label: "ရွေးချယ်ထားသော Applio အသံမော်ဒယ်",
        applio_status_active: "🟢 Laptop Worker အဆင်သင့်ဖြစ်ပါသည်",
        applio_status_waiting: "🟡 Laptop Worker စောင့်ဆိုင်းနေပါသည် (Queue)",

        queue_notice_voxcpm: "⚡ ပုံမှန်ခန့်မှန်းကြာချိန်: ၅-၁၅ စက္ကန့် • Live Cloud AI စနစ်",
        queue_notice_applio: "⚡ Local GPU မော်ဒယ်များ: Laptop Worker ဖွင့်ထားချိန်တွင် ချက်ချင်း ဆောင်ရွက်ပေးပါသည်",

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
        recap_url_label: "YouTube, TikTok သို့မဟုတ် Douyin (抖音) Video Link",
        recap_url_placeholder: "YouTube, TikTok, Douyin link (https://v.douyin.com/...)...",
        btn_analyze: "စစ်ဆေးမည်",
        recap_analyzing: "ဗီဒီယို အချက်အလက် စစ်ဆေးနေပါသည်...",

        // Aspect Ratio & Layout
        recap_ratio_label: "ဗီဒီယို အချိုးအစား (Aspect Ratio):",
        ratio_9_16: "📐 9:16 (TikTok / Reels / Shorts)",
        ratio_16_9: "📐 16:9 (YouTube / အလျားလိုက်)",
        ratio_1_1: "📐 1:1 (Square စကွဲယား)",
        ratio_original: "📐 Original (မူရင်းအချိုးအစား)",

        recap_fill_label: "ဘောင်ဖြည့်စွက်မှု (Aspect Fill):",
        fill_blur: "🌫️ Blur Background (ဝါးဖြည့်)",
        fill_crop: "✂️ Center Crop (အလယ်ဖြတ်)",
        fill_black: "⬛ Black Bars (ဘေးဘောင်မည်း)",

        // Custom Video Crop
        recap_crop_label: "ဗီဒီယို ဘောင်စိတ်ကြိုက်ဖြတ်ခြင်း (Custom Video Crop):",
        crop_sync_label: "အပေါ်/အောက် တပြိုင်နက်",
        crop_preset_full: "📐 မူရင်း (0%)",
        crop_preset_cinema: "🎞️ CinemaScope (12%)",
        crop_preset_side: "↔️ ဘေးဘောင်ဖြတ် (10%)",
        crop_preset_custom: "✂️ စိတ်ကြိုက်",
        crop_top_label: "↕️ အပေါ်ဘောင်ဖြတ် (Top Crop):",
        crop_bottom_label: "↕️ အောက်ဘောင်ဖြတ် (Bottom Crop):",
        crop_left_label: "↔️ ဘယ်ဘက်ဖြတ် (Left Crop):",
        crop_right_label: "↔️ ညာဘက်ဖြတ် (Right Crop):",

        // Anti-Copyright Protections
        recap_anticopyright_label: "မူပိုင်ခွင့် ကာကွယ်ရေး (Anti-Copyright):",
        recap_mirror_label: "🪞 Video Mirror (ဘယ်/ညာ လှန်မည်):",
        mirror_on: "🪞 Mirror: ON ✅",
        mirror_off: "🪞 Mirror: OFF ❌",
        recap_zoom_label: "🔍 Micro-Zoom (ဇူးမ်ချဲ့ခြင်း):",
        zoom_off: "1.0x ပုံမှန်",
        zoom_std: "1.06x စံနှုန်း 🔥",
        zoom_strong: "1.10x အမြင့်",

        // Voice Speed & Pacing
        recap_speed_label: "ဇာတ်လမ်းပြောနှုန်း (Narration Speed / Pacing):",
        speed_0: "+0% ပုံမှန်",
        speed_8: "+8% သွက်",
        speed_12: "+12% အကြံပြု 🔥",
        speed_18: "+18% မြန်",
        speed_22: "+22% အထူးမြန်",

        // Toggles
        recap_burn_subs_label: "📝 စာတန်းထိုး ထည့်သွင်းခြင်း:",
        recap_blur_subs_label: "🌫️ မူရင်းစာတန်းဖျက် Blur:",
        toggle_on: "ON ✅",
        toggle_off: "OFF ❌",

        // Branding Logo
        recap_logo_toggle_label: "⭐ Channel Logo Watermark:",
        recap_logo_pos_label: "Logo နေရာ:",
        pos_top_right: "အပေါ် ညာ",
        pos_top_left: "အပေါ် ဘယ်",
        pos_bottom_right: "အောက် ညာ",
        pos_bottom_left: "အောက် ဘယ်",

        recap_style_label: "ဇာတ်လမ်းပြော စတိုင်လ် (Story Style):",
        style_viral: "🔥 Viral Hook (စိတ်လှုပ်ရှားဖွယ်)",
        style_dramatic: "🎭 သည်းထိတ်ရင်ဖို (Drama)",
        style_funny: "😂 ဟာသရယ်မောဖွယ်",
        style_pov: "👁️ ဇာတ်ကောင် POV",
        style_standard: "📖 ပုံမှန်ဇာတ်ကြောင်း",

        preview_label: "🖼️ Real-time Preview မျက်နှာပြင်",
        preview_hint: "Slider များကို ရွှေ့၍ နေရာညှိပါ",
        blur_box_label: "မူရင်းစာတန်းဖျက် Blur Box",
        sub_sample_text: "\"အခုတော့ ကျွန်တော်တို့ရဲ့ အစီအစဉ် စတင်ပါပြီ...\"",
        logo_label: "⭐ LOGO",
        slider_blur_y: "🔲 မူရင်းစာတန်းဖျက် Blur အနိမ့်အမြင့် (Y):",
        slider_blur_h: "↕️ Blur Box အထူ/အမြင့် (H):",
        slider_sub_y: "📝 မြန်မာစာတန်းထိုး နေရာ (Sub Y):",

        sub_style_label: "စာတန်းထိုး စတိုင်လ် (Subtitle Style):",
        sub_style_yellow: "🟡 Classic Gold (အဝါ)",
        sub_style_white: "⚪ Clean White (အဖြူ)",
        sub_style_boxed: "⬛ Dark Box (နောက်ခံ)",
        sub_style_neon: "🔵 Neon Cyan (နီယွန်)",

        sub_font_size_label: "စာလုံးအရွယ်အစား:",
        sub_lines_label: "စာကြောင်းရေ:",

        recap_voice_label: "Recap အတွက် အသုံးပြုမည့် အသံ (Dub Voice)",
        queue_notice_recap: "⚡ Movie Recap ခန့်မှန်းကြာချိန်: ၁-၂ မိနစ် (Full HD 1080p Render)",
        btn_start_recap: "⚡ 1-Click Pro Recap ဖန်တီးမည် (1 Credit)",

        // Deliverables
        recap_result_title: "🎉 Pro Movie Recap ဖန်တီးမှု ပြီးစီးပါပြီ!",
        btn_dl_recap_video: "🎬 Download Full HD Video (MP4)",
        btn_dl_recap_audio: "🎙️ Download Voiceover (MP3)",
        btn_dl_recap_srt: "📝 Download Subtitles (SRT)",
        btn_dl_recap_script: "📜 Download Script (TXT)",
        recap_processing_msg: "Recap စတင်လုပ်ဆောင်နေပါသည်...",

        // Tab 3: Downloader
        dl_title: "📥 Media Downloader",
        dl_subtitle: "YouTube, TikTok နှင့် Douyin (抖音) မှ Video (MP4)၊ Audio (MP3) ဒေါင်းလုဒ် ရယူပါ",
        dl_url_label: "Video သို့မဟုတ် Music Link ကူးထည့်ပါ",
        dl_url_placeholder: "YouTube, TikTok, Douyin (抖音) URL...",
        btn_search_dl: "🔍 ဒေါင်းလုဒ် ရွေးချယ်မှုများ ရှာဖွေမည်",
        fmt_video_title: "Full HD Video",
        fmt_video_desc: "MP4 (H.264 / AAC)",
        fmt_audio_title: "High-Quality Audio",
        fmt_audio_desc: "MP3 (192kbps)",
        fmt_sub_title: "Subtitle File",
        fmt_sub_desc: "SRT (Auto-captions)",

        // Tab 4: VIP Plans
        vip_title: "💎 Creator VIP Plans & Wallet",
        vip_subtitle: "အရည်အသွေးမြင့်မားသော စတူဒီယို အစီအစဉ်များကို စိတ်ကြိုက်ရွေးချယ်ပါ",
        stat_current_tier: "လက်ရှိ အဆင့်အတန်း",
        stat_balance_min: "အသံသွင်းခွင့် လက်ကျန် (Minutes)",
        stat_recap_credits: "Recap Video လက်ကျန် (Credits)",

        plan_voice_name: "🎙️ Voice Clone Studio Pro",
        plan_voice_alloc: "မိနစ် ၃၀၀ (၅ နာရီ) စတူဒီယို အသံသွင်းခွင့်",
        plan_voice_price: "25,000",
        plan_voice_badge: "Voice Creators 🔥",
        plan_voice_f1: "✓ VoxCPM Cloud AI အသံ ၁၂ မျိုး အသုံးပြုခွင့်",
        plan_voice_f2: "✓ Custom Voice Clone (အသံဖိုင် Upload တင်၍ တူအောင်ပွားခွင့်)",
        plan_voice_f3: "✓ Applio RVC Local GPU မော်ဒယ်များ အသုံးပြုခွင့်",
        plan_voice_f4: "✓ High-Speed Priority Cloud Processing Queue",
        plan_voice_f5: "✓ အရည်အသွေးမြင့် 24kHz PCM WAV တိုက်ရိုက်ဒေါင်းလုဒ်",

        plan_recap_name: "🎬 Movie Recap Studio Pro",
        plan_recap_alloc: "ဗီဒီယို ၆၀ ကား Recap ဖန်တီးခွင့် (၁ ပုဒ် အများဆုံး ၁၀ မိနစ်)",
        plan_recap_price: "35,000",
        plan_recap_badge: "Video Editors 🎬",
        plan_recap_f1: "✓ Full HD 1080p Movie Recap ဗီဒီယို (၆၀) ကား (အများဆုံး ၁၀ မိနစ်/ကား)",
        plan_recap_f2: "✓ ဇာတ်လမ်းစတိုင် ၅ မျိုး & စာတန်းထိုးစတိုင် ၄ မျိုး",
        plan_recap_f3: "✓ မူရင်းစာတန်းဖျက် Smart Blur (Y, H စိတ်ကြိုက်ချိန်ညှိ)",
        plan_recap_f4: "✓ Channel Watermark Logo ထည့်သွင်းခွင့်",
        plan_recap_f5: "✓ High-Speed Multi-Threaded Video Rendering",

        plan_master_name: "👑 All-in-One Master Suite",
        plan_master_alloc: "မိနစ် ၆၀၀ + Recap ဗီဒီယို ၁၅၀ ကား (အများဆုံး ၁၀ မိနစ်/ကား)",
        plan_master_price: "55,000",
        plan_master_badge: "အကောင်းဆုံး တန်ဖိုး 💎",
        plan_master_f1: "✓ Voice Clone Studio Pro အပြည့်အစုံ (မိနစ် ၆၀၀ - ၁၀ နာရီ)",
        plan_master_f2: "✓ Movie Recap Studio Pro အပြည့်အစုံ (ဗီဒီယို ၁၅၀ ကား - အများဆုံး ၁၀ မိနစ်/ကား)",
        plan_master_f3: "✓ မိမိကိုယ်ပိုင် Clone Voice Model (၁) ခု အခမဲ့ Train ပေးခြင်း",
        plan_master_f4: "✓ Dedicated High-Speed Cloud Node Priority",
        plan_master_f5: "✓ Commercial Copyright Safe & 24/7 VIP Direct Support",

        // Recap Duration Limit UI
        recap_limit_notice: "🎬 ၁ Credit = ဗီဒီယို ၁ ပုဒ် (အများဆုံး ၁၀ မိနစ်အထိ)",
        recap_duration_warning: "⚠️ ဗီဒီယို ကြာချိန် ၁၀ မိနစ်ထက် ကျော်လွန်နေပါသည် (၁ Credit လျှင် အများဆုံး ၁၀ မိနစ်သာ ခွင့်ပြုထားပါသည်)",
        recap_duration_valid: "✅ ၁၀ မိနစ်အတွင်း အကျုံးဝင်သည်",

        btn_buy_voice: "💎 Voice Plan ဝယ်ယူမည်",
        btn_buy_recap: "🎬 Recap Plan ဝယ်ယူမည်",
        btn_buy_master: "👑 Master Suite ဝယ်ယူမည်",

        payment_info_title: "💳 ငွေပေးချေမှု အချက်အလက်များ",
        copy_btn: "📋 ကူးမည်",
        payment_notice: "⚠️ ငွေလွှဲပြီးပါက ငွေလွှဲပြေစာ Screenshot ကို အောက်တွင် တင်သွင်းပေးပါ သို့မဟုတ် @VoiceCloneProAdmin သို့ ပို့ပေးပါရန်။",
        slip_upload_title: "📸 ငွေလွှဲပြေစာ (Screenshot) တင်သွင်းရန်",
        btn_submit_slip: "📤 ပြေစာ တင်သွင်းမည်",

        // Tab 5: Admin Console
        admin_title: "⚡ Executive Control Center",
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
        btn_add_recap_credit: "🎬 +10 Recap Credit ထည့်မည်",
        admin_broadcast_header: "📢 Telegram Bot အားလုံးသို့ Broadcast စာပို့ရန်",
        admin_broadcast_ph: "အသုံးပြုသူ အားလုံးထံ ပေးပို့မည့် စာသား ရိုက်ထည့်ပါ...",
        btn_send_broadcast: "🚀 Broadcast ပို့မည်",

        // Admin Live Queue & Settings
        admin_queue_header: "⚡ Live Processing Queue & အလုပ်များ စစ်ဆေးခြင်း",
        admin_queue_loading: "Queue ရှိ အလုပ်များကို ရှာဖွေနေပါသည်...",
        admin_queue_empty: "ရွေးချယ်ထားသော အခြေအနေနှင့် ကိုက်ညီသည့် အလုပ် မရှိသေးပါ",
        admin_queue_idle_title: "🟢 Queue အခြေအနေ ရှင်းလင်းနေပါသည် (0 Pending)",
        admin_queue_idle_desc: "လက်ရှိ Queue ထဲတွင် စောင့်ဆိုင်းနေသော အလုပ်မရှိပါ။ Local GPU Worker (Awin) သည် အသင့်အနေအထား (Standby) တွင် ရှိနေပါသည်။ အောက်တွင် ဖော်ပြထားသော အလုပ်များသည် အောင်မြင်စွာ ပြီးစီးပြီးသား မှတ်တမ်းများ (Completed History) ဖြစ်ပါသည်။",
        admin_queue_running_title: "⚡ Local GPU Worker သည် အလုပ်များကို စတင်လုပ်ဆောင်နေပါသည် (Running)",
        admin_queue_queued_title: "⏳ အလုပ်များ Queue ထဲတွင် စောင့်ဆိုင်းနေပါသည် (Waiting)",
        admin_history_section_title: "📜 အောင်မြင်စွာ ပြီးစီးပြီးသား မှတ်တမ်းများ (Completed History)",
        admin_active_section_title: "🔥 လက်ရှိ လုပ်ဆောင်ဆဲ နှင့် စောင့်ဆိုင်းဆဲ အလုပ်များ (Active Tasks)",
        badge_completed: "✅ ပြီးစီးပြီး",
        badge_queued: "⏳ စောင့်ဆိုင်းဆဲ",
        badge_running: "⚡ လုပ်ဆောင်ဆဲ",
        badge_failed: "❌ မအောင်မြင်ပါ",
        filter_all: "အားလုံး",
        filter_queued: "⏳ စောင့်ဆိုင်းဆဲ",
        filter_running: "⚡ အလုပ်လုပ်နေဆဲ",
        filter_success: "✅ အောင်မြင်ပြီး",
        filter_failed: "❌ မအောင်မြင်ပါ",
        admin_settings_header: "⚙️ စနစ်ဆိုင်ရာ ဒေတာ အချက်အလက်များ ပြင်ဆင်ခြင်း",
        settings_kpay_num: "KBZPay ဖုန်းနံပါတ်",
        settings_kpay_name: "KBZPay အမည်",
        settings_wave_num: "WavePay ဖုန်းနံပါတ်",
        settings_wave_name: "WavePay အမည်",
        settings_tutorial_link: "လမ်းညွှန်ဗီဒီယို Link",
        settings_maintenance: "စနစ်ပြုပြင်ထိန်းသိမ်းမှု မုဒ် (Maintenance)",
        btn_save_settings: "💾 အချက်အလက်များ သိမ်းဆည်းမည်",
        btn_retry_job: "🔄 ထပ်မံ ကြိုးစားမည်",
        btn_cancel_job: "⏹️ ပယ်ဖျက်မည်",
        btn_preview_voice: "🔊 နမူနာ နားထောင်မည်",
        preview_voice: "နမူနာ နားထောင်မည်",
        preview_playing: "ဖွင့်နေသည်...",
        alert_voice_ready: "🎉 သင့် AI Voice ဖန်တီးမှု အောင်မြင်စွာ ပြီးစီးပါပြီ!",
        alert_recap_ready: "🎉 Pro Movie Recap ဖန်တီးမှု အောင်မြင်စွာ ပြီးစီးပါပြီ!",

        // 24-Hour History UI
        history_recap_title: "🕒 ၂၄ နာရီအတွင်း Recap ဖန်တီးမှုများ",
        history_voice_title: "🕒 ၂၄ နာရီအတွင်း Voice ဖန်တီးမှုများ",
        history_expiry_note: "⏱️ ဖိုင်များကို ၂၄ နာရီအတွင်းသာ သိမ်းဆည်းထားပြီးနောက် အလိုအလျောက် ပယ်ဖျက်ပါမည်",
        history_empty: "လတ်တလော ၂၄ နာရီအတွင်း ဖန်တီးထားသော ဖိုင် မရှိသေးပါ",
        admin_history_header: "📊 အသုံးပြုသူအားလုံး၏ ၂၄ နာရီ ထုတ်ယူမှု မှတ်တမ်း",
        admin_history_note: "⏱️ User များနှင့် Admin တို့၏ ၂၄ နာရီအတွင်း ထုတ်ယူထားသော အသံနှင့် ဗီဒီယို ဖိုင်များအားလုံး (၂၄ နာရီကျော်ပါက အလိုအလျောက် ပယ်ဖျက်သည်)",
        admin_history_purge_btn: "🧹 ၂၄ နာရီကျော်ဖိုင်များ ရှင်းမည်",

        // Notifications & Toasts
        toast_copied: "ကူးယူပြီးပါပြီခင်ဗျာ!",
        toast_empty_text: "ကျေးဇူးပြု၍ စာသား ရိုက်ထည့်ပေးပါခင်ဗျာ။",
        toast_empty_url: "ကျေးဇူးပြု၍ Video URL ထည့်သွင်းပေးပါခင်ဗျာ။",
        toast_success_slip: "ပြေစာ အောင်မြင်စွာ တင်သွင်းပြီးပါပြီ။",
        btn_send_voice_telegram: "📱 Telegram သို့ ပို့မည်",
        btn_send_recap_telegram: "📱 Telegram သို့ ပို့မည်",
        btn_telegram_sent: "✅ Telegram သို့ ပို့ပြီးပါပြီ",
        toast_telegram_sent: "Telegram သို့ အောင်မြင်စွာ ပေးပို့ပြီးပါပြီခင်ဗျာ!",
        toast_sending_telegram: "Telegram သို့ ပေးပို့နေပါသည်...",
    },

    en: {
        // App Top Bar & Brand
        brand_title: "VOICE CLONE PRO",
        brand_subtitle: "STUDIO • ULTRA SUITE",
        btn_tutorial: "Tutorial",
        tutorial_banner_title: "Tutorial Video Guide",
        tutorial_banner_desc: "Watch step-by-step video guide for Voice Cloning",
        recap_tutorial_title: "Movie Recap Guide",
        recap_tutorial_desc: "Learn how to generate movie recaps with Myanmar narration",
        btn_watch: "Watch ▶️",
        vip_free: "FREE CREATOR",
        vip_purchased: "👑 VIP PRO",
        unlimited: "Unlimited",
        balance_voice_label: "🎙️ Voice Allowance:",
        balance_recap_label: "🎬 Recap Credits:",

        // Bottom Navigation Tabs (English)
        tab_voice: "Voice",
        tab_recap: "Recap",
        tab_downloader: "Downloader",
        tab_vip: "VIP Plans",
        tab_admin: "Admin",

        // Tab 1: Voice Clone Studio
        voice_title: "🎙️ AI Voice Clone Studio",
        voice_subtitle: "High-Fidelity Myanmar Speech Synthesis & Voice Cloning",
        engine_label: "Select AI Synthesis Engine",
        engine_voxcpm_title: "☁️ VoxCPM (Cloud AI)",
        engine_voxcpm_desc: "Zero-shot natural speech • Real-time cloud",
        engine_applio_title: "💻 Applio RVC (Local GPU)",
        engine_applio_desc: "Local trained models on laptop worker",

        persona_mode_title: "Voice Selection Mode",
        btn_mode_curated: "🌟 Curated Personas (12)",
        btn_mode_custom: "🎙️ Custom Voice Clone",

        curated_section_title: "Pre-tuned Gemini Voice Personas (12)",
        custom_ref_title: "🎙️ Reference Voice (Clone Sample)",
        custom_ref_optional: "Optional",
        custom_ref_desc: "Upload target voice sample (3-30s WAV/MP3). If left empty, default Burmese AI voice will be used.",
        ref_file_btn: "📁 Choose Reference Audio File",
        btn_clear_file: "✕ Clear",

        model_picker_label: "Selected Applio Voice Model",
        applio_status_active: "🟢 Laptop Worker Online",
        applio_status_waiting: "🟡 Waiting for Laptop Worker (Queued)",

        queue_notice_voxcpm: "⚡ Estimated Duration: 5-15s • Real-time Cloud AI Node",
        queue_notice_applio: "⚡ Local GPU Models: Processed instantly when Laptop Worker is active",

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
        recap_url_label: "YouTube, TikTok or Douyin (抖音) Video Link",
        recap_url_placeholder: "YouTube, TikTok, Douyin link (https://v.douyin.com/...)...",
        btn_analyze: "Analyze Link",
        recap_analyzing: "Analyzing video stream...",

        // Aspect Ratio & Layout
        recap_ratio_label: "Video Aspect Ratio:",
        ratio_9_16: "📐 9:16 (TikTok / Reels / Shorts)",
        ratio_16_9: "📐 16:9 (YouTube / Widescreen)",
        ratio_1_1: "📐 1:1 (Instagram / Square)",
        ratio_original: "📐 Original (Source Ratio)",

        recap_fill_label: "Aspect Ratio Fill Mode:",
        fill_blur: "🌫️ Blur Background (Recommended)",
        fill_crop: "✂️ Center Crop",
        fill_black: "⬛ Black Letterbox",

        // Custom Video Crop
        recap_crop_label: "Custom Video Crop:",
        crop_sync_label: "Sync Top/Bottom",
        crop_preset_full: "📐 Original (0%)",
        crop_preset_cinema: "🎞️ CinemaScope (12%)",
        crop_preset_side: "↔️ Side Crop (10%)",
        crop_preset_custom: "✂️ Custom",
        crop_top_label: "↕️ Top Crop:",
        crop_bottom_label: "↕️ Bottom Crop:",
        crop_left_label: "↔️ Left Crop:",
        crop_right_label: "↔️ Right Crop:",

        // Anti-Copyright Protections
        recap_anticopyright_label: "Anti-Copyright Protection:",
        recap_mirror_label: "🪞 Video Mirror (H-Flip):",
        mirror_on: "🪞 Mirror: ON ✅",
        mirror_off: "🪞 Mirror: OFF ❌",
        recap_zoom_label: "🔍 Micro-Zoom:",
        zoom_off: "1.0x None",
        zoom_std: "1.06x Standard 🔥",
        zoom_strong: "1.10x Strong",

        // Voice Speed & Pacing
        recap_speed_label: "Narration Speed / Pacing:",
        speed_0: "+0% Normal",
        speed_8: "+8% Brisk",
        speed_12: "+12% Recommended 🔥",
        speed_18: "+18% Fast",
        speed_22: "+22% Ultra-fast",

        // Toggles
        recap_burn_subs_label: "📝 Burn Myanmar Subtitles:",
        recap_blur_subs_label: "🌫️ Subtitle Blur Box:",
        toggle_on: "ON ✅",
        toggle_off: "OFF ❌",

        // Branding Logo
        recap_logo_toggle_label: "⭐ Channel Logo Watermark:",
        recap_logo_pos_label: "Logo Position:",
        pos_top_right: "Top-Right",
        pos_top_left: "Top-Left",
        pos_bottom_right: "Bottom-Right",
        pos_bottom_left: "Bottom-Left",

        recap_style_label: "Narration Story Style:",
        style_viral: "🔥 Viral Hook (High Engagement)",
        style_dramatic: "🎭 Dramatic (Suspense Thriller)",
        style_funny: "😂 Comedy (Humorous)",
        style_pov: "👁️ First-Person POV",
        style_standard: "📖 Classic Storytelling",

        preview_label: "🖼️ Real-time Canvas Preview",
        preview_hint: "Adjust sliders to position overlays",
        blur_box_label: "Subtitle Deletion Blur Box",
        sub_sample_text: "\"Now our story officially begins...\"",
        logo_label: "⭐ LOGO",
        slider_blur_y: "🔲 Subtitle Blur Position (Y):",
        slider_blur_h: "↕️ Blur Box Height (H):",
        slider_sub_y: "📝 Subtitle Vertical Position (Sub Y):",

        sub_style_label: "Subtitle Typography Style:",
        sub_style_yellow: "🟡 Classic Gold",
        sub_style_white: "⚪ Clean White",
        sub_style_boxed: "⬛ Dark Box",
        sub_style_neon: "🔵 Neon Cyan",

        sub_font_size_label: "Font Size:",
        sub_lines_label: "Lines:",

        recap_voice_label: "Narrator Dubbing Voice",
        queue_notice_recap: "⚡ Movie Recap Estimate: 1-2 mins (Full HD 1080p Render)",
        btn_start_recap: "⚡ Generate 1-Click Pro Recap (1 Credit)",

        // Deliverables
        recap_result_title: "🎉 Pro Movie Recap Generated Successfully!",
        btn_dl_recap_video: "🎬 Download Full HD Video (MP4)",
        btn_dl_recap_audio: "🎙️ Download Voiceover (MP3)",
        btn_dl_recap_srt: "📝 Download Subtitles (SRT)",
        btn_dl_recap_script: "📜 Download Script (TXT)",
        recap_processing_msg: "Processing recap video...",

        // Tab 3: Downloader
        dl_title: "📥 Media Downloader",
        dl_subtitle: "Download high-speed MP4 Video, MP3 Audio from YouTube, TikTok and Douyin (抖音)",
        dl_url_label: "Paste Video or Audio Link",
        dl_url_placeholder: "YouTube, TikTok, Douyin (抖音) URL...",
        btn_search_dl: "🔍 Fetch Download Formats",
        fmt_video_title: "Full HD Video",
        fmt_video_desc: "MP4 (H.264 / AAC)",
        fmt_audio_title: "High-Quality Audio",
        fmt_audio_desc: "MP3 (192kbps)",
        fmt_sub_title: "Subtitle File",
        fmt_sub_desc: "SRT (Auto-captions)",

        // Tab 4: VIP Plans
        vip_title: "💎 Creator VIP Plans & Wallet",
        vip_subtitle: "Premium creator-tier subscriptions built for serious editors & creators",
        stat_current_tier: "Current Membership Tier",
        stat_balance_min: "Remaining Minutes",
        stat_recap_credits: "Remaining Recap Credits",

        plan_voice_name: "🎙️ Voice Clone Studio Pro",
        plan_voice_alloc: "300 Usable Minutes (5 Hours Audio)",
        plan_voice_price: "25,000",
        plan_voice_badge: "Voice Creators 🔥",
        plan_voice_f1: "✓ 12 VoxCPM Cloud AI Personas (6M / 6F)",
        plan_voice_f2: "✓ Custom Voice Clone (Audio Sample Upload)",
        plan_voice_f3: "✓ Applio RVC Local GPU Trained Models",
        plan_voice_f4: "✓ High-Speed Priority Cloud Processing Queue",
        plan_voice_f5: "✓ High-Fidelity 24kHz PCM WAV Direct Download",

        plan_recap_name: "🎬 Movie Recap Studio Pro",
        plan_recap_alloc: "60 Movie Recap Credits (Max 10 mins/video)",
        plan_recap_price: "35,000",
        plan_recap_badge: "Video Editors 🎬",
        plan_recap_f1: "✓ 60 Full HD 1080p Movie Recap Generations (Max 10 mins/video)",
        plan_recap_f2: "✓ 5 Story Styles & 4 Subtitle Typography Presets",
        plan_recap_f3: "✓ Anti-Copyright Smart Blur Box (Y, H sliders)",
        plan_recap_f4: "✓ Custom Channel Logo Watermark Overlay",
        plan_recap_f5: "✓ High-Speed Multi-Threaded Video Rendering",

        plan_master_name: "👑 All-in-One Master Suite",
        plan_master_alloc: "600 Mins + 150 Recap Credits (Max 10 mins/video)",
        plan_master_price: "55,000",
        plan_master_badge: "Best Value Master 💎",
        plan_master_f1: "✓ Full Voice Clone Studio Pro (600 Mins)",
        plan_master_f2: "✓ Full Movie Recap Studio Pro (150 Videos - Max 10 mins/video)",
        plan_master_f3: "✓ 1 Free Custom Trained Voice Model Slot",
        plan_master_f4: "✓ Dedicated High-Speed Cloud Node Priority",
        plan_master_f5: "✓ Commercial Copyright Safe & 24/7 VIP Support",

        // Recap Duration Limit UI
        recap_limit_notice: "🎬 1 Credit = 1 Video (Max 10 mins duration)",
        recap_duration_warning: "⚠️ Video duration exceeds 10 minutes limit (1 Credit allows up to 10 mins)",
        recap_duration_valid: "✅ Within 10 mins limit",

        btn_buy_voice: "💎 Get Voice Plan",
        btn_buy_recap: "🎬 Get Recap Plan",
        btn_buy_master: "👑 Get Master Suite",

        payment_info_title: "💳 Payment Account Details",
        copy_btn: "📋 Copy",
        payment_notice: "⚠️ After payment, upload your transfer screenshot below or send to @VoiceCloneProAdmin for instant activation.",
        slip_upload_title: "📸 Upload Payment Screenshot",
        btn_submit_slip: "📤 Submit Receipt Slip",

        // Tab 5: Admin Console
        admin_title: "⚡ Executive Control Center",
        admin_subtitle: "Operations overview, payment verification, and user management",
        admin_stat_users: "Total Registered Users",
        admin_stat_vip: "VIP Members",
        admin_stat_slips: "Pending Slips",
        admin_stat_queue: "Queued Jobs",
        admin_slips_header: "📋 Payment Transfer Slips Awaiting Review",
        admin_no_slips: "No pending payment slips",
        btn_approve: "✓ Approve (+VIP)",
        btn_reject: "✕ Reject",
        admin_user_manage_header: "👤 User Tier & Allowance Management",
        admin_user_id_ph: "Telegram ID (e.g. 1957002115)",
        btn_grant_vip: "👑 Upgrade to VIP",
        btn_add_minutes: "⏱️ Add +60 Minutes",
        btn_add_recap_credit: "🎬 Add +10 Recap Credits",
        admin_broadcast_header: "📢 Broadcast Message to All Users",
        admin_broadcast_ph: "Type broadcast message to deliver to all users...",
        btn_send_broadcast: "🚀 Send Broadcast",

        // Admin Live Queue & Settings
        admin_queue_header: "⚡ Live Processing Queue & Job Inspector",
        admin_queue_loading: "Loading live processing queue...",
        admin_queue_empty: "No jobs match the selected filter criteria",
        admin_queue_idle_title: "🟢 Queue is Clear (0 Pending Tasks)",
        admin_queue_idle_desc: "There are currently 0 pending tasks waiting in the queue. Local GPU Worker (Awin) is online, healthy, and on standby. The jobs listed below are past successfully completed tasks (Completed History).",
        admin_queue_running_title: "⚡ Local Worker is Actively Processing Jobs (Running)",
        admin_queue_queued_title: "⏳ Jobs Waiting in Queue (Waiting)",
        admin_history_section_title: "📜 Completed Job History",
        admin_active_section_title: "🔥 Active Queue Tasks",
        badge_completed: "✅ COMPLETED",
        badge_queued: "⏳ WAITING",
        badge_running: "⚡ RUNNING",
        badge_failed: "❌ FAILED",
        filter_all: "All",
        filter_queued: "⏳ Queued",
        filter_running: "⚡ Running",
        filter_success: "✅ Success",
        filter_failed: "❌ Failed",
        admin_settings_header: "⚙️ Dynamic Platform Settings",
        settings_kpay_num: "KBZPay Phone Number",
        settings_kpay_name: "KBZPay Account Name",
        settings_wave_num: "WavePay Phone Number",
        settings_wave_name: "WavePay Account Name",
        settings_tutorial_link: "Tutorial Video Link",
        settings_maintenance: "Maintenance Mode",
        btn_save_settings: "💾 Save Platform Settings",
        btn_retry_job: "🔄 Retry Job",
        btn_cancel_job: "⏹️ Cancel",
        btn_preview_voice: "🔊 Preview Voice",
        preview_voice: "Listen Preview",
        preview_playing: "Playing...",
        alert_voice_ready: "🎉 Your AI Voice Generation is Ready!",
        alert_recap_ready: "🎉 Pro Movie Recap Generation is Ready!",

        // 24-Hour History UI
        history_recap_title: "🕒 24-Hour Recap Productions",
        history_voice_title: "🕒 24-Hour Voice Productions",
        history_expiry_note: "⏱️ Media files are retained for 24 hours, then auto-purged",
        history_empty: "No creations in the last 24 hours",
        admin_history_header: "📊 All Users 24-Hour Job Production Monitor",
        admin_history_note: "⏱️ All voice & recap jobs produced within the past 24 hours (auto-purged after 24h)",
        admin_history_purge_btn: "🧹 Purge Expired Files (>24h)",

        // Notifications & Toasts
        toast_copied: "Copied to clipboard!",
        toast_empty_text: "Please enter some text to synthesize.",
        toast_empty_url: "Please enter a valid video URL.",
        toast_success_slip: "Payment slip submitted successfully.",
        btn_send_voice_telegram: "📱 Send to Telegram",
        btn_send_recap_telegram: "📱 Send to Telegram",
        btn_telegram_sent: "✅ Sent to Telegram",
        toast_telegram_sent: "Delivered to Telegram successfully!",
        toast_sending_telegram: "Delivering to Telegram...",
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
