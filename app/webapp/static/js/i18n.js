/**
 * i18n.js - Comprehensive Localization Engine for Studio Pro Mini App
 * Pure Burmese (my) and Pure English (en) without mixed or hardcoded strings.
 */

const translations = {
    my: {
        // App Top Bar & Brand
        brand_title: "VOICE CLONE",
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

        persona_mode_title: "အသံရွေးချယ်မှု စတိုင်လ်",
        btn_mode_curated: "🌟 Curated Voices (၁၂ မျိုး)",
        btn_mode_custom: "🎙️ Custom Voice Clone",

        curated_section_title: "ကြိုတင်သတ်မှတ်ထားသော Gemini အသံများ (၁၂ မျိုး)",
        custom_ref_title: "🎙️ Reference Voice (အသံတူ Clone ရန် အသံနမူနာ)",
        custom_ref_optional: "ရွေးချယ်ခွင့် (Optional)",
        custom_ref_desc: "မိမိပြောလိုသော အသံပိုင်ရှင်၏ အသံဖိုင် (၃-၃၀ စက္ကန့် WAV/MP3) တင်သွင်းနိုင်ပါသည်။ မတင်ပါက Default Burmese AI Voice ဖြင့် ပြောပါမည်။",
        ref_file_btn: "📁 Reference Audio ဖိုင် ရွေးချယ်မည်",
        btn_clear_file: "✕ ဖျက်မည်",

        voxcpm_status_waiting: "🟡 VoxCPM2 Queue စောင့်ဆိုင်းနေပါသည်",

        queue_notice_voxcpm: "⚡ ပုံမှန်ခန့်မှန်းကြာချိန်: ၅-၁၅ စက္ကန့် • Live Cloud AI စနစ်",

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
        recap_step1_title: "ဗီဒီယိုလင့်ခ် ထည့်သွင်းပါ (Video Link)",
        recap_step2_title: "ဇာတ်လမ်းပြောမည့် မြန်မာအသံ ရွေးချယ်ပါ (Dub Voice)",
        advanced_settings_title: "စိတ်ကြိုက် အသေးစိတ် ပြင်ဆင်ရန် (Advanced Customization)",
        advanced_settings_sub: "Aspect Ratio၊ မူပိုင်ခွင့်ကာကွယ်ရေးနှင့် Live Canvas နေရာညှိရန်",
        queue_notice_recap: "⚡ High-Speed Studio: Shorts (၁-၃ မိနစ်) ~ ၁.၅ မိနစ် • Cinema (၅-၁၀ မိနစ်) ~ ၃-၄ မိနစ် (Groq Whisper Turbo + 1080p Cinema Compositing)",
        telegram_bg_assurance: "✨ ဖုန်း Screen ရှေ့တွင် စောင့်မနေပါနှင့် — Cloud AI Render ပြီးစီးပါက Telegram Bot ထံသို့ တိုက်ရိုက် ပို့ဆောင်ပေးပါမည်။",
        telegram_leave_hint: "💡 ဤစာမျက်နှာကို ပိတ်၍ ထွက်သွားနိုင်ပါသည်။ Video အဆင်သင့်ဖြစ်ပါက Telegram Bot က သင့်ထံ အကြောင်းကြားစာနှင့် ဗီဒီယို ပို့ပေးပါမည်။",
        recap_eta_text: "ခန့်မှန်းကြာချိန်: ၂-၃ မိနစ် (Groq Turbo Ingestion + Neural Smart Sync)",
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
        plan_voice_alloc: "မိနစ် ၂၅၀ (၄ နာရီကျော်) စတူဒီယို အသံသွင်းခွင့်",
        plan_voice_price: "75,000",
        plan_voice_badge: "Voice Creators 🔥",
        plan_voice_f1: "✓ VoxCPM Cloud AI မြန်မာအသံ ၁၂ မျိုး (၆ ကျား / ၆ မ) အပြည့်အစုံ",
        plan_voice_f2: "✓ Custom Voice Clone (အသံဖိုင် Upload တင်၍ တူအောင်ပွားခွင့်)",
        plan_voice_f3: "✓ VoxCPM2 Reference Audio (၃-၃၀ စက္ကန့်) နဲ့ အသံတူပွားခွင့်",
        plan_voice_f4: "✓ High-Speed Priority Cloud Processing Queue",
        plan_voice_f5: "✓ အရည်အသွေးမြင့် 24kHz PCM Lossless WAV တိုက်ရိုက်ဒေါင်းလုဒ်",
        plan_voice_f6: "✓ တစ်ကြိမ်လျှင် စာလုံးရေ ၅၀၀၀ အထိ အကန့်အသတ်မရှိ ထုတ်လုပ်နိုင်ခွင့်",

        plan_recap_name: "🎬 Movie Recap Studio Pro",
        plan_recap_alloc: "ဗီဒီယို ၂၅ ကား Recap ဖန်တီးခွင့် (၁ ပုဒ် အများဆုံး ၁၀ မိနစ်)",
        plan_recap_price: "90,000",
        plan_recap_badge: "Cinema Suite 🎬",
        plan_recap_f1: "✓ Full HD 1080p Movie Recap ဗီဒီယို (၂၅) ကား (အများဆုံး ၁၀ မိနစ်/ကား)",
        plan_recap_f2: "✓ 🛡️ 100% Anti-Copyright Smart Shield (Smart Blur, Auto-Mirror & Zoom)",
        plan_recap_f3: "✓ ဇာတ်လမ်းစတိုင် ၅ မျိုး & ရွှေရောင်စာတန်းထိုးစတိုင် ၄ မျိုး",
        plan_recap_f4: "✓ Watermark လုံးဝမပါ (Clean 1080p) & Channel Logo သီးသန့်ထည့်ခွင့်",
        plan_recap_f5: "✓ Multi-Threaded High-Speed Video Rendering Node",

        plan_master_name: "👑 All-in-One Master Suite",
        plan_master_alloc: "မိနစ် ၅၀၀ + Recap ဗီဒီယို ၆၀ ကား (အများဆုံး ၁၀ မိနစ်/ကား)",
        plan_master_price: "180,000",
        plan_master_badge: "Agency Master Suite 👑",
        plan_master_f1: "✓ Voice Clone Studio Pro အပြည့်အစုံ (မိနစ် ၅၀၀ - ၈ နာရီကျော်)",
        plan_master_f2: "✓ Movie Recap Studio Pro အပြည့်အစုံ (ဗီဒီယို ၆၀ ကား - အများဆုံး ၁၀ မိနစ်/ကား)",
        plan_master_f3: "✓ 👑 VoxCPM2 Reference Audio အသံတူပွားမှု (Custom Clone) အပြည့်အစုံ",
        plan_master_f4: "✓ 🛡️ Anti-Copyright Smart Suite & Commercial Monetization Safe",
        plan_master_f5: "✓ Dedicated High-Speed Node Priority & 24/7 VIP Direct Support",

        // Three plan categories (2026-10-29): Voice-only / Recap-only / Combined
        plan_cat_voice: "🎙️ Voice-only — အသံသီးသန့်",
        plan_cat_recap: "🎬 Recap-only — Recap သီးသန့်",
        plan_cat_combined: "🎙️🎬 Combined Voice+Recap — နှစ်မျိုးလုံး",
        plan_packs_badge: "Voice Packs 💎",
        plan_packs_name: "🎙️ Voice Minute Packs",
        plan_packs_alloc: "မိနစ် ၁၀ / ၃၀ / ၆၀ — လိုသလောက်ဝယ်၊ သက်တမ်းမကုန်",
        plan_packs_price: "8,000",
        plan_packs_from: "မှစ၍",
        plan_packs_f1: "✓ မိနစ် ၁၀ — ၈,၀၀၀ ကျပ် (၈၀၀/မိနစ်)",
        plan_packs_f2: "✓ မိနစ် ၃၀ — ၂၀,၀၀၀ ကျပ် (၆၆၇/မိနစ်)",
        plan_packs_f3: "✓ မိနစ် ၆၀ — ၃၈,၀၀၀ ကျပ် (၆၃၃/မိနစ်)",
        plan_packs_btn10: "💎 ၁၀ မိနစ် ဝယ်ယူမည် — ၈,၀၀၀",
        plan_packs_btn30: "⭐ ၃၀ မိနစ် ဝယ်ယူမည် — ၂၀,၀၀၀",
        plan_packs_btn60: "🔥 ၆၀ မိနစ် ဝယ်ယူမည် — ၃၈,၀၀၀",
        plan_recap_starter_badge: "Recap Starter 🎬",
        plan_recap_starter_name: "🎬 Movie Recap Starter",
        plan_recap_starter_alloc: "ဗီဒီယို ၅ ကား Recap ဖန်တီးခွင့် (၁ ပုဒ် အများဆုံး ၁၀ မိနစ်)",
        plan_recap_starter_price: "20,000",
        plan_recap_starter_f1: "✓ Full HD 1080p Movie Recap ဗီဒီယို (၅) ကား (အများဆုံး ၁၀ မိနစ်/ကား)",
        plan_recap_starter_f2: "✓ 🛡️ 100% Anti-Copyright Smart Shield (Smart Blur, Auto-Mirror & Zoom)",
        plan_recap_starter_f3: "✓ Watermark လုံးဝမပါ (Clean 1080p)",
        btn_buy_recap_starter: "🎬 Recap Starter ဝယ်ယူမည်",
        plan_combined_starter_badge: "Combined Starter ✨",
        plan_combined_starter_name: "🎙️🎬 Voice + Recap Starter",
        plan_combined_starter_alloc: "မိနစ် ၃၀ + Recap ဗီဒီယို ၅ ကား (အများဆုံး ၁၀ မိနစ်/ကား)",
        plan_combined_starter_price: "30,000",
        plan_combined_starter_f1: "✓ Voice မိနစ် ၃၀ + Recap ဗီဒီယို ၅ ကား — နှစ်မျိုးလုံးကို တစ်ပြိုင်တည်း စမ်းသုံးနိုင်သည်",
        plan_combined_starter_f2: "✓ VoxCPM2 Reference Audio (၃-၃၀ စက္ကန့်) နဲ့ အသံတူပွားခွင့်",
        plan_combined_starter_f3: "✓ 🛡️ Anti-Copyright Smart Shield & Watermark လုံးဝမပါ",
        btn_buy_combined_starter: "✨ Combined Starter ဝယ်ယူမည်",
        // Recap Duration Limit UI
        recap_limit_notice: "🎬 အခမဲ့အသုံးပြုသူ: အများဆုံး ၂ မိနစ် (Watermark ပါဝင်မည်) • VIP: ၁၀ မိနစ်အထိ (No Watermark)",
        recap_duration_warning: "⚠️ ကြာချိန်ကန့်သတ်ချက်ထက် ကျော်လွန်နေပါသည် (VIP ရယူပါ)",
        recap_duration_valid: "✅ ကြာချိန် သတ်မှတ်ချက်အတွင်း အကျုံးဝင်ပါသည်",

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
        recap_fallback_note: "⚠️ စာကြောင်း {count} ကြောင်းကို VoxCPM အစား အရန် Neural Audio (Edge TTS) ဖြင့် ထုတ်ပေးထားပါသည်။",
        voice_fallback_notice: "⚠️ VoxCPM အစား အရန် Neural Audio (Edge TTS) ဖြင့် ထုတ်ပေးထားပါသည်။",

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
        pause_audio: "ခေတ္တရပ်မည် (Pause Audio)",
        play_audio: "အသံဖွင့်နားထောင်မည် (Play Audio)",
        replay_audio: "ပြန်လည်နားထောင်မည် (Replay)",
        worker_auto_complete: "Laptop Worker ဖွင့်ထားပါက အလိုအလျောက် ပြီးစီးပါမည်",
        cloud_queue_waiting: "⏳ Cloud Queue စောင့်ဆိုင်းနေပါသည်...",
        tutorial_coming_soon: "⚠️ {label} လမ်းညွှန်ဗီဒီယိုကို မကြာမီ တင်ဆက်ပေးပါမည်။",
        text_too_long: "စာသား စာလုံးရေ ({len}) သည် {tier} အသုံးပြုသူ ကန့်သတ်ချက် ({max} လုံး) ထက် ကျော်လွန်နေပါသည်။{vip_note}",
        gate_banner_title: "🔒 Channel နှင့် Group ကို အရင် Join ပါ",
        gate_banner_desc: "Join ပြီးမှ Verify နှိပ်ပါ — ပြီးမှ Voice / Recap ထုတ်နိုင်မယ်။",
        gate_not_configured_title: "⚠️ Gate မပြင်ဆင်ရသေးပါ — Admin ကို ဆက်သွယ်ပါ",
        gate_not_configured_desc: "Channel / Group ID တွေ server မှာ မရှိသေးလို့ အတည်ပြုလို့ မရသေးပါ။",
        gate_checking: "စစ်နေပါသည်…",
        gate_not_member: "မဝင်ရသေးပါ — Channel နှင့် Group နှစ်ခုလုံး Join ထားမှ ရမယ်။",
        gate_verified_toast: "✅ အတည်ပြုပြီးပါပြီ — အခု ထုတ်နိုင်ပါပြီ",
        gate_not_found_toast: "Channel / Group မဝင်ရသေးပါ",
        gate_check_failed: "စစ်ဆေးမှု မအောင်မြင်ပါ — ပြန်ကြိုးစားပါ။",
        gate_join_channel: "Channel Join",
        gate_join_group: "Group Join",
        gate_verify_btn: "✓ Verify လုပ်မယ်",
        plan_how_title: "💎 Plan ဘယ်လို အလုပ်လုပ်လဲ",
        plan_how_text1: "၁) Plan ရွေးပါ → ၂) KBZPay / WavePay နဲ့ ငွေလွှဲပြီး Slip တင်ပါ → ၃) Admin အတည်ပြုပြီးမှ မိနစ် / Recap Credit ဝင်မယ်။ အခမဲ့ (Free) အဆင့်ကနေ ဝယ်ယူပြီးတာနဲ့ Purchased (VIP) ဖြစ်သွားမယ် — တစ်ကြိမ်စာလုံးရေ ၁၀၀၀ ကနေ ၅၀၀၀ အထိ တက်မယ်၊ Recap ၂ မိနစ်ကနေ ၁၀ မိနစ်အထိ တက်မယ်၊ Watermark ပျောက်မယ်။",
        plan_how_text2: "စျေးနှုန်းအပြည့်အစုံကို VIP tab မှာ ကြည့်နိုင်ပါတယ် — နောက်ဆုံး စျေးနှုန်းကို အတည်ပြုပြီးမှ UI နဲ့ backend မှာ တစ်ပြိုင်တည်း ပြောင်းပါမယ်။",
        preflight_line: "✍️ {len} / {max} လုံး • 🎙️ လက်ကျန် {bal} မိနစ်",
        preflight_over_limit: " • ⚠️ ကန့်သတ်ချက် ကျော်နေတယ်",
        preflight_balance_empty: " • ⚠️ လက်ကျန် ကုန်နေတယ် — VIP tab မှာ ဝယ်ပါ",
        open_via_telegram_title: "📱 Telegram ထဲမှ ဖွင့်ပါ",
        open_via_telegram_desc: "ဤ App ကို Telegram Bot ထဲမှ Mini App အဖြစ် ဖွင့်မှ သာ အသုံးပြုနိုင်ပါသည်။ Browser ထဲမှ တိုက်ရိုက်ဖွင့်လျှင် အကောင့် အတည်ပြုခြင်း မအောင်မြင်ပါ။",
        open_via_telegram_btn: "နားလည်ပါပြီ",

    },
    en: {
        // App Top Bar & Brand
        brand_title: "VOICE CLONE",
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

        persona_mode_title: "Voice Selection Mode",
        btn_mode_curated: "🌟 Curated Personas (12)",
        btn_mode_custom: "🎙️ Custom Voice Clone",

        curated_section_title: "Pre-tuned Gemini Voice Personas (12)",
        custom_ref_title: "🎙️ Reference Voice (Clone Sample)",
        custom_ref_optional: "Optional",
        custom_ref_desc: "Upload target voice sample (3-30s WAV/MP3). If left empty, default Burmese AI voice will be used.",
        ref_file_btn: "📁 Choose Reference Audio File",
        btn_clear_file: "✕ Clear",

        voxcpm_status_waiting: "🟡 Waiting for VoxCPM2 Queue",

        queue_notice_voxcpm: "⚡ Estimated Duration: 5-15s • Real-time Cloud AI Node",

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
        recap_step1_title: "Step 1: Paste Video Link",
        recap_step2_title: "Step 2: Choose Narrator Voice",
        advanced_settings_title: "Advanced Studio Settings",
        advanced_settings_sub: "Aspect Ratio, Anti-Copyright & Canvas Repositioning",
        queue_notice_recap: "⚡ High-Speed Studio: Shorts (1-3m) ~ 1.5 mins • Cinema (5-10m) ~ 3-4 mins (Groq Whisper Turbo + 1080p Cinema Compositing)",
        telegram_bg_assurance: "✨ No need to wait on screen — Finished video will be delivered directly to your Telegram Bot.",
        telegram_leave_hint: "💡 You may safely close or leave this page. Once rendering completes, the Telegram Bot will deliver your video and deliverables.",
        recap_eta_text: "Estimated time: 2-3 mins (Groq Turbo Ingestion + Neural Smart Sync)",
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
        plan_voice_alloc: "250 Usable Minutes (4+ Hours Audio)",
        plan_voice_price: "75,000",
        plan_voice_badge: "Voice Creators 🔥",
        plan_voice_f1: "✓ 12 VoxCPM Cloud AI Personas (6M / 6F)",
        plan_voice_f2: "✓ Up to 5,000 characters per generation (Custom Clone Upload Included)",
        plan_voice_f3: "✓ VoxCPM2 Reference Audio Cloning (3-30s sample)",
        plan_voice_f4: "✓ High-Speed Priority Cloud Processing Queue",
        plan_voice_f5: "✓ High-Fidelity 24kHz PCM Lossless WAV Direct Download",
        plan_voice_f6: "✓ Unlimited Daily Generations with 5,000 Char Quota",

        plan_recap_name: "🎬 Movie Recap Studio Pro",
        plan_recap_alloc: "25 Movie Recap Credits (Max 10 mins/video)",
        plan_recap_price: "90,000",
        plan_recap_badge: "Cinema Suite 🎬",
        plan_recap_f1: "✓ 25 Full HD 1080p Movie Recap Generations (Max 10 mins/video)",
        plan_recap_f2: "✓ 🛡️ 100% Anti-Copyright Smart Shield (Smart Blur, Auto-Mirror & Zoom)",
        plan_recap_f3: "✓ 5 Story Styles & 4 Luxury Subtitle Typography Presets",
        plan_recap_f4: "✓ 100% Watermark-Free (Clean 1080p) & Custom Channel Logo Support",
        plan_recap_f5: "✓ Multi-Threaded High-Speed Video Rendering Node",

        plan_master_name: "👑 All-in-One Master Suite",
        plan_master_alloc: "500 Mins + 60 Recap Credits (Max 10 mins/video)",
        plan_master_price: "180,000",
        plan_master_badge: "Agency Master Suite 👑",
        plan_master_f1: "✓ Full Voice Clone Studio Pro (500 Mins - 8+ Hours)",
        plan_master_f2: "✓ Full Movie Recap Studio Pro (60 Videos - Max 10 mins/video)",
        plan_master_f3: "✓ 👑 Full VoxCPM2 Reference Audio Custom Cloning",
        plan_master_f4: "✓ 🛡️ 100% Commercial Copyright Safe & Monetization Ready",
        plan_master_f5: "✓ Dedicated High-Speed Cloud Node Priority & 24/7 VIP Direct Support",

        // Three plan categories (2026-10-29): Voice-only / Recap-only / Combined
        plan_cat_voice: "🎙️ Voice-only",
        plan_cat_recap: "🎬 Recap-only",
        plan_cat_combined: "🎙️🎬 Combined Voice+Recap",
        plan_packs_badge: "Voice Packs 💎",
        plan_packs_name: "🎙️ Voice Minute Packs",
        plan_packs_alloc: "10 / 30 / 60 minutes — buy what you need, never expires",
        plan_packs_price: "8,000",
        plan_packs_from: "from",
        plan_packs_f1: "✓ 10 minutes — 8,000 MMK (800/min)",
        plan_packs_f2: "✓ 30 minutes — 20,000 MMK (667/min)",
        plan_packs_f3: "✓ 60 minutes — 38,000 MMK (633/min)",
        plan_packs_btn10: "💎 Buy 10 Minutes — 8,000",
        plan_packs_btn30: "⭐ Buy 30 Minutes — 20,000",
        plan_packs_btn60: "🔥 Buy 60 Minutes — 38,000",
        plan_recap_starter_badge: "Recap Starter 🎬",
        plan_recap_starter_name: "🎬 Movie Recap Starter",
        plan_recap_starter_alloc: "5 Movie Recap Generations (Max 10 mins/video)",
        plan_recap_starter_price: "20,000",
        plan_recap_starter_f1: "✓ 5 Full HD 1080p Movie Recap Generations (Max 10 mins/video)",
        plan_recap_starter_f2: "✓ 🛡️ 100% Anti-Copyright Smart Shield (Smart Blur, Auto-Mirror & Zoom)",
        plan_recap_starter_f3: "✓ Zero Watermark (Clean 1080p)",
        btn_buy_recap_starter: "🎬 Buy Recap Starter",
        plan_combined_starter_badge: "Combined Starter ✨",
        plan_combined_starter_name: "🎙️🎬 Voice + Recap Starter",
        plan_combined_starter_alloc: "30 Mins + 5 Recap Videos (Max 10 mins/video)",
        plan_combined_starter_price: "30,000",
        plan_combined_starter_f1: "✓ 30 Voice Minutes + 5 Recap Videos — try both products in one plan",
        plan_combined_starter_f2: "✓ VoxCPM2 Reference Audio Cloning (3-30s sample)",
        plan_combined_starter_f3: "✓ 🛡️ Anti-Copyright Smart Shield & Zero Watermark",
        btn_buy_combined_starter: "✨ Buy Combined Starter",
        // Recap Duration Limit UI
        recap_limit_notice: "🎬 Free Creator: Max 2 Mins (Watermark Included) • VIP: Up to 10 Mins (No Watermark)",
        recap_duration_warning: "⚠️ Video duration exceeds tier limit (Upgrade to VIP)",
        recap_duration_valid: "✅ Within allowed duration limit",

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
        recap_fallback_note: "⚠️ {count} narration line(s) were generated with the backup Neural Audio (Edge TTS) instead of VoxCPM.",
        voice_fallback_notice: "⚠️ This audio was generated with the backup Neural Audio (Edge TTS) instead of VoxCPM.",

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
        pause_audio: "Pause Audio",
        play_audio: "Play Audio",
        replay_audio: "Replay Audio",
        worker_auto_complete: "It will complete automatically if the Laptop Worker is running",
        cloud_queue_waiting: "⏳ Waiting in Cloud Queue...",
        tutorial_coming_soon: "⚠️ {label} tutorial video is coming soon.",
        text_too_long: "Text length ({len}) exceeds your {tier} limit of {max} characters.{vip_note}",
        gate_banner_title: "🔒 Join the Channel and Group first",
        gate_banner_desc: "Join both, then tap Verify — only then can you generate Voice / Recap.",
        gate_not_configured_title: "⚠️ Membership gate is not configured — please contact the admin",
        gate_not_configured_desc: "Channel / Group IDs are not set on the server yet, so verification is unavailable.",
        gate_checking: "Checking…",
        gate_not_member: "Not a member yet — please join both the Channel and the Group.",
        gate_verified_toast: "✅ Verified — you can generate now",
        gate_not_found_toast: "Channel / Group membership not found",
        gate_check_failed: "Verification failed — please try again.",
        gate_join_channel: "Channel Join",
        gate_join_group: "Group Join",
        gate_verify_btn: "✓ Verify",
        plan_how_title: "💎 How plans work",
        plan_how_text1: "1) Choose a plan → 2) Transfer with KBZPay / WavePay and submit your slip → 3) Minutes / Recap Credits arrive only after admin approval. Once you buy, you move from Free to Purchased (VIP) — per-job characters rise from 1000 to 5000, Recap from 2 minutes to 10 minutes, and the watermark is removed.",
        plan_how_text2: "See the VIP tab for full final prices.",
        preflight_line: "✍️ {len} / {max} chars • 🎙️ Balance {bal} min",
        preflight_over_limit: " • ⚠️ Over your character limit",
        preflight_balance_empty: " • ⚠️ Balance is empty — buy in the VIP tab",
        open_via_telegram_title: "📱 Open via Telegram",
        open_via_telegram_desc: "This app only works when opened as a Mini App inside the Telegram bot. Opening it directly in a browser cannot verify your account.",
        open_via_telegram_btn: "Got it",
    }
};

let currentLang = localStorage.getItem("studio_lang") || "my";

function t(key) {
    const dict = translations[currentLang] || translations.my;
    return dict[key] || translations.en[key] || key;
}

// tf(key, params): t() with {placeholder} interpolation for dynamic messages.
function tf(key, params) {
    let msg = t(key);
    if (params) {
        for (const [k, v] of Object.entries(params)) {
            msg = msg.split("{" + k + "}").join(String(v));
        }
    }
    return msg;
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
