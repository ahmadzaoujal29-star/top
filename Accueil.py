import streamlit as st
import json
import os
import time
# 🌟 إضافة مكتبة Gemini SDK
from google import genai
from google.genai.errors import APIError
import bcrypt
from PIL import Image
from io import BytesIO
from datetime import date
from supabase import create_client, Client
from streamlit_cookies_manager import EncryptedCookieManager
from urllib.parse import urlparse, parse_qs

# --- I. Configuration Globale ---

st.set_page_config(
    page_title="Tuteur IA Mathématiques (Système Marocain)",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Constantes et Secrets
MAX_REQUESTS = 5
REFERRAL_BONUS = 10
REFERRAL_PARAM = "ref_code"
COOKIE_KEY_EMAIL = "user_auth_email"
SUPABASE_TABLE_NAME = "users"
ADMIN_EMAIL = "ahmed.tantawi.10@gmail.com" # استخدم بريدك الإلكتروني هنا

# Configuration des API Keys depuis secrets.toml
try:
    API_KEY = st.secrets["GEMINI_API_KEY"]
    SUPABASE_URL: str = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY: str = st.secrets["SUPABASE_KEY"]
    SERVICE_KEY = st.secrets.get("SUPABASE_SERVICE_KEY")
except KeyError as e:
    st.error(f"Erreur de configuration: Clé manquante dans secrets.toml: {e}. L'application ne démarrera pas correctement.")
    st.stop()
    
# 🌟 تهيئة عميل Gemini SDK
try:
    GEMINI_CLIENT = genai.Client(api_key=API_KEY)
except Exception as e:
    st.error(f"Erreur d'initialisation Gemini SDK: {e}")
    st.stop()

# قائمة المستويات التعليمية المغربية
MAROC_LEVELS = [
    'الإعدادي (Collège)',
    'جذع مشترك (Tronc Commun)',
    'الأولى بكالوريا (1ère Année Bac)',
    'الثانية بكالوريا (2ème Année Bac)',
    'الدروس الخصوصية (Classes Préparatoires)',
]

# 🌟 الإضافة الجديدة: خيارات أنواع الاستجابة
RESPONSE_TYPES = {
    'steps': 'Étapes Détaillées (Didactique)',
    'concept': 'Explication Conceptuelle (Théorie)',
    'answer': 'Réponse Finale (Concise)'
}


# --- II. Initialisation des Clients et de l'État ---

# 1. Initialisation des Cookies
cookies = EncryptedCookieManager(
    prefix="gemini_math_app/",
    password=st.secrets.get("COOKIE_PASSWORD", "super_secret_default_key"),
)
if not cookies.ready():
    st.stop()

# 2. Initialisation Supabase Client
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    users_table = supabase.table(SUPABASE_TABLE_NAME)
except Exception as e:
    st.error(f"Erreur d'initialisation Supabase: {e}")
    st.stop()
    
# 3. Initialisation de l'État de la Session
if 'auth_status' not in st.session_state: st.session_state.auth_status = 'logged_out'
if 'user_email' not in st.session_state: st.session_state.user_email = None
if 'user_data' not in st.session_state: st.session_state.user_data = None
if 'requests_today' not in st.session_state: st.session_state.requests_today = 0
if 'is_unlimited' not in st.session_state: st.session_state.is_unlimited = False
if 'should_rerun' not in st.session_state: st.session_state.should_rerun = False
if 'school_level' not in st.session_state: st.session_state.school_level = MAROC_LEVELS[-1] 
if 'response_type' not in st.session_state: st.session_state.response_type = 'steps'
if 'lang' not in st.session_state: st.session_state.lang = 'fr'


# --- III. Fonctions de Base (Supabase & Crypto) ---

def get_supabase_client(use_service_key: bool = False) -> Client:
    """Retourne le client Supabase standard ou le client avec clé de service."""
    if use_service_key and SERVICE_KEY:
        return create_client(SUPABASE_URL, SERVICE_KEY)
    return supabase

def hash_password(password: str) -> str:
    """Hachage sécurisé du mot المرور بـ bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def check_password(password: str, hashed_password: str) -> bool:
    """Vérifie le mot المرور المدخل."""
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))
    except Exception:
        return False

def get_user_by_email(email: str):
    """Récupère les données utilisateur."""
    try:
        response = users_table.select("*").eq("email", email).limit(1).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Erreur de récupération utilisateur: {e}")
        return None

def update_user_data(email, data: dict, use_service_key=False):
    """Met à jour les données utilisateur."""
    try:
        client_to_use = get_supabase_client(use_service_key)
        response = client_to_use.table(SUPABASE_TABLE_NAME).update(data).eq("email", email).execute()
        
        if response.data and st.session_state.user_email == email:
            # Mise à jour de la session si l'utilisateur actuel est modifié
            st.session_state.user_data.update(response.data[0])
        return True
    except Exception as e:
        print(f"Erreur de mise à jour Supabase: {e}")
        return False


# --- IV. Logique de l'API Gemini ---

def build_system_prompt():
    """
    Construit la System Instruction complète.
    Inclut des instructions strictes de formatage pour des réponses Tidy/Clean.
    """
    # استخدام القيم من session_state مباشرة (التي تم تحديثها في load_user_session)
    school_level = st.session_state.school_level
    response_type = st.session_state.response_type
    lang = st.session_state.lang

    # Base: Spécialisation et niveau
    base_prompt = (
        f"Tu es un tuteur spécialisé en mathématiques, expert du système éducatif marocain (niveau {school_level}). "
        "Ta mission est de fournir une assistance précise et didactique. Si une image est fournie, tu dois l'analyser et résoudre le problème. "
        "Si une image est fournie, commence par une description concise du problème (en utilisant la langue de réponse choisie) avant de passer à la résolution structurée."
    )
    
    # Style de réponse (inclut des instructions de clarté spécifiques)
    if response_type == 'answer':
        style_instruction = "Fournis uniquement la réponse finale et concise du problème, sans aucune explication détaillée ni étapes intermédiaires. Mets la réponse en gras et clairement en évidence."
    elif response_type == 'concept':
        style_instruction = "Fournis une explication conceptuelle approfondie du problème ou du sujet. Concentre-toi sur les théories et les concepts impliqués, et utilise des sous-titres clairs pour séparer les notions."
    else: # 'steps' par défaut
        style_instruction = "Fournis les étapes détaillées de résolution de manière structurée et méthodique, en utilisant une liste numérotée pour chaque étape majeure du raisonnement."

    # Langue
    lang_instruction = "Tu dois répondre exclusivement en français." if lang == 'fr' else "Tu dois répondre exclusivement en arabe، في تنسيق (Markdown) وباستخدام المصطلحات الرياضية المعتادة في المغرب."

    # Instruction STRICTE de mise en forme (Tidiness/Clarity)
    formatting_instruction = (
        "Réponds IMPÉRATIVEMENT en utilisant une structure **Markdown** claire (titres, listes, gras). "
        "Toutes les expressions mathématiques complexes, symboles, formules ou équations doivent être écrites UNIQUEMENT en **LaTeX**. "
        "Utilise le format LaTeX : encadre les équations en ligne avec '$' et les blocs d'équations avec '$$'. "
        "Il est INTERDIT d'utiliser du texte brut، des barres obliques (/) ou des accents circonflexes (^) pour représenter des fractions, des exposants ou des symboles mathématiques dans la réponse finale."
    )
    
    # Instruction finale complète
    final_prompt = (
        f"{base_prompt} {lang_instruction} {style_instruction} {formatting_instruction}"
    )
    return final_prompt

def stream_text_simulation(text):
    """Simule la frappe de texte pour une meilleure UX."""
    for chunk in text.split():
        yield chunk + " "
        time.sleep(0.02)

# 🌟 دالة call_gemini_api المُحدَّثة لاستخدام SDK 🌟
def call_gemini_api(prompt: str, uploaded_file=None):
    """Appelle l'API Gemini en utilisant le SDK."""
    
    email = st.session_state.user_email
    user_data = st.session_state.user_data
    current_date_str = str(date.today())
    
    # 1. Vérification des Limites (Logiciel inchangé)
    max_total_requests = MAX_REQUESTS + user_data.get('bonus_questions', 0)
    if not user_data.get('is_unlimited', False):
        
        # Réinitialisation du compteur si la date a changé
        if user_data.get('last_request_date') != current_date_str:
            st.session_state.requests_today = 0
            update_user_data(email, {'requests_today': 0, 'last_request_date': current_date_str})

        current_count = st.session_state.requests_today

        if current_count >= max_total_requests:
            st.error(f"Limite atteinte: Vous avez atteint le maximum de requêtes ({max_total_requests}) pour aujourd'hui. Revenez demain!")
            return "Limite de requêtes atteinte.", []
            
        st.session_state.requests_today = current_count + 1 # Incrément avant l'appel

    # 2. بناء الـ Contents والتعليمات المخصصة
    final_system_prompt = build_system_prompt()
    contents = []
    
    if uploaded_file is not None:
        try:
            # SDK يستقبل كائن PIL.Image مباشرة
            uploaded_file.seek(0) 
            image = Image.open(uploaded_file)
            contents.append(image)
        except Exception:
            return "تعذّر معالجة الصورة. تأكد من أن التنسيق هو JPG أو PNG.", []
    
    if prompt: 
        contents.append(prompt)
        
    if not contents:
        return "Veuillez fournir une question ou une image.", []

    # 3. الاتصال بالـ API باستخدام SDK
    try:
        response = GEMINI_CLIENT.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents,
            # تمرير System Instruction و Tools عبر Config
            config={
                "system_instruction": final_system_prompt,
                "tools": [{"google_search": {} }]
            }
        )
        
        # 4. تحديث العداد في Supabase بعد النجاح
        if not user_data.get('is_unlimited', False):
            update_user_data(email, {'requests_today': st.session_state.requests_today, 'last_request_date': current_date_str})
            
        # 5. استخراج الإجابة والمصادر
        generated_text = response.text
        
        sources = []
        if (response.candidates and 
            response.candidates[0].grounding_metadata and 
            hasattr(response.candidates[0].grounding_metadata, 'grounding_attributions')):

            for attribution in response.candidates[0].grounding_metadata.grounding_attributions:
                if hasattr(attribution, 'web') and attribution.web and attribution.web.title:
                    sources.append({
                        'uri': attribution.web.uri,
                        'title': attribution.web.title
                    })

        return generated_text, sources

    except APIError as e:
        st.error(f"❌ Erreur API (Code {e.code}): {e.message}")
        return f"Échec de l'API Gemini (Code {e.code}). Cause probable: {e.message}", []
        
    except Exception as e:
        st.error(f"خطأ غير متوقع: {e}")
        return f"خطأ غير متوقع: {e}", []

# --- V. Fonctions d'Authentification et de Session ---

def load_user_session(email, save_cookie=False):
    """Charge les données utilisateur et met à jour la session."""
    user_data = get_user_by_email(email)
    
    if user_data:
        if save_cookie:
            cookies[COOKIE_KEY_EMAIL] = email
            cookies.save()
            
        st.session_state.user_email = email
        st.session_state.user_data = user_data
        
        # نسخ مفاتيح الإعدادات الأساسية مباشرة إلى st.session_state
        st.session_state.school_level = user_data.get('school_level', MAROC_LEVELS[-1])
        st.session_state.response_type = user_data.get('response_type', 'steps')
        st.session_state.lang = user_data.get('lang', 'fr')
        
        # Chargement des préférences utilisateur
        st.session_state.is_unlimited = user_data.get('is_unlimited', False)
        
        # Gestion du compteur quotidien 
        current_date_str = str(date.today())
        if user_data.get('last_request_date') != current_date_str:
            st.session_state.requests_today = 0
        else:
            st.session_state.requests_today = user_data.get('requests_today', 0)
            
        st.session_state.auth_status = 'logged_in'
        st.session_state.should_rerun = True
        return True
    return False

def handle_login():
    """Gère la connexion."""
    email = st.session_state.login_email.lower()
    password = st.session_state.login_password
    
    user_data = get_user_by_email(email)
    
    if user_data and check_password(password, user_data.get('password_hash', '')):
        st.success("Connexion réussie! Bienvenue.")
        load_user_session(email, save_cookie=True)
    else:
        st.error("E-mail ou mot de passe incorrect.")

def handle_register():
    """Gère l'inscription و Parrainage."""
    email = st.session_state.reg_email.lower()
    password = st.session_state.reg_password
    confirm_password = st.session_state.reg_password_confirm
    # استخلاص خيارات التخصيص من النموذج
    selected_level = st.session_state.reg_level
    selected_lang = st.session_state.reg_lang
    selected_response_type = st.session_state.reg_response_type
    
    if password != confirm_password:
        st.error("Les mots de passe ne correspondent pas.")
        return
    if len(password) < 6:
        st.error("Le mot de passe doit contenir au moins 6 caractères.")
        return
        
    if get_user_by_email(email):
        st.error("Cet e-mail est déjà enregistré. Veuillez vous connecter.")
        return

    # Logique de Parrainage
    referrer_email = None
    query_params = st.query_params
    
    if REFERRAL_PARAM in query_params:
        potential_referrer_email = query_params.get(REFERRAL_PARAM)
        if isinstance(potential_referrer_email, list): potential_referrer_email = potential_referrer_email[0]
            
        referrer_data = get_user_by_email(potential_referrer_email)
        if referrer_data and referrer_data['email'] != email: 
            referrer_email = potential_referrer_email
            current_bonus = referrer_data.get('bonus_questions', 0)
            new_bonus = current_bonus + REFERRAL_BONUS
            
            # Utilisation de la clé de service pour l'opération d'écriture (plus sûr)
            if update_user_data(referrer_email, {'bonus_questions': new_bonus}, use_service_key=True):
                st.info(f"Félicitations! Le parrain ({referrer_email}) a reçu {REFERRAL_BONUS} questions bonus.")
            
    # Sauvegarder le nouvel utilisateur
    new_user_data = {
        'email': email,
        'password_hash': hash_password(password),
        'lang': selected_lang,
        'school_level': selected_level, 
        'response_type': selected_response_type, 
        'is_unlimited': False,
        'requests_today': 0,
        'last_request_date': str(date.today()),
        'bonus_questions': 0,
        'referred_by': referrer_email,
    }
    
    try:
        users_table.insert([new_user_data]).execute()
        st.success("Inscription et connexion réussيت! 🥳")
        load_user_session(email, save_cookie=True)
    except Exception as e:
        st.error(f"Échec de l'inscription: {e}. (Vérifiez les règles RLS de Supabase.)")


# --- VI. Interface Utilisateur (UI) ---

def admin_dashboard_ui():
    """واجهة الإدارة تظهر فقط للبريد الإلكتروني المخصص."""
    st.sidebar.markdown("---")
    st.sidebar.subheader("👑 لوحة تحكم الإدارة")
    st.sidebar.warning("هذا القسم مرئي فقط لك.")
    
    st.sidebar.markdown(f"**بريد الإدارة:** `{ADMIN_EMAIL}`")
    
    # مثال على زر لإعطاء وصول غير محدود لنفسك
    if st.sidebar.button("تفعيل/إلغاء الوصول غير المحدود"):
        is_current_unlimited = st.session_state.user_data.get('is_unlimited', False)
        new_status = not is_current_unlimited
        
        # استخدام مفتاح الخدمة للتأكد من التحديث
        if update_user_data(ADMIN_EMAIL, {'is_unlimited': new_status}, use_service_key=True):
            st.session_state.is_unlimited = new_status
            st.session_state.should_rerun = True
            st.sidebar.success(f"حالة الوصول غير المحدود: {'مُفعل' if new_status else 'مُلغى'}")
        else:
            st.sidebar.error("فشل التحديث. تأكد من إعداد SUPABASE_SERVICE_KEY.")
            
    st.sidebar.markdown("---")


def auth_ui():
    """Interface de connexion/inscription."""
    st.header("🔑 Connexion / Inscription")
    st.markdown("---")

    col1, col2 = st.columns(2)
    
    with col1:
        with st.form("login_form"):
            st.subheader("Se Connecter")
            st.text_input("E-mail", key="login_email")
            st.text_input("Mot de passe", type="password", key="login_password")
            st.form_submit_button("Connexion", type="primary", on_click=handle_login)

    with col2:
        with st.form("register_form"):
            st.subheader("S'inscrire")
            st.text_input("E-mail", key="reg_email")
            st.text_input("Mot de passe", type="password", key="reg_password")
            st.text_input("Confirmer le mot de passe", type="password", key="reg_password_confirm")
            
            st.subheader("Vos Préférences (Initiales)")
            
            # حقل المستوى الدراسي
            st.selectbox(
                "Niveau Scolaire (Système Marocain)",
                options=MAROC_LEVELS,
                index=len(MAROC_LEVELS) - 1, 
                key="reg_level"
            )
            
            # حقل اللغة
            st.radio(
                "Langue de Réponse",
                options=["fr", "ar"],
                format_func=lambda x: "Français 🇫🇷" if x == "fr" else "العربية 🇲🇦",
                key="reg_lang",
                horizontal=True
            )
            
            # حقل نوع الاستجابة
            st.selectbox(
                "Type de Réponse par Défaut",
                options=list(RESPONSE_TYPES.keys()),
                format_func=lambda x: RESPONSE_TYPES[x],
                index=0, # steps
                key="reg_response_type",
                help="Choisissez comment l'IA devrait répondre par défaut (Étapes, Concept, ou Réponse Finale)."
            )


            query_params = st.query_params
            if REFERRAL_PARAM in query_params:
                ref_email = query_params.get(REFERRAL_PARAM)
                if isinstance(ref_email, list): ref_email = ref_email[0]
                st.info(f"Vous vous inscrivez via le lien de parrainage ({ref_email}). Votre parrain recevra un bonus!")

            st.form_submit_button("S'inscrire", type="secondary", on_click=handle_register)


# --- NOUVEAU: Fonctions de l'interface d'édition des paramètres ---

def update_preference(key):
    """
    Met à jour une préférence utilisateur dans la session et dans Supabase 
    en utilisant la clé de session correspondante.
    """
    new_value = st.session_state[f'setting_{key}']
    
    st.session_state[key] = new_value 
    
    data_to_update = {key: new_value}
    
    if update_user_data(st.session_state.user_email, data_to_update):
        st.session_state.user_data[key] = new_value 
        st.sidebar.success(f"Préférence mise à jour: {key}")
    else:
        st.sidebar.error("Échec de la sauvegarde. Veuillez réessayer.")

def settings_ui():
    """Interface utilisateur pour gérer les préférences de l'utilisateur dans la sidebar."""
    st.sidebar.header("⚙️ Mes Préférences (AI Output)")
    
    # Niveau Scolaire
    st.sidebar.selectbox(
        "Niveau Scolaire (affecte la difficulté)",
        options=MAROC_LEVELS,
        index=MAROC_LEVELS.index(st.session_state.school_level),
        key="setting_school_level",
        on_change=lambda: update_preference('school_level')
    )
    
    # Langue de Réponse
    st.sidebar.radio(
        "Langue de Réponse",
        options=["fr", "ar"],
        format_func=lambda x: "Français 🇫🇷" if x == "fr" else "العربية 🇲🇦",
        key="setting_lang",
        index=0 if st.session_state.lang == 'fr' else 1,
        on_change=lambda: update_preference('lang'),
        horizontal=True
    )
    
    # Type de Réponse (Tidiness/Clarity)
    st.sidebar.selectbox(
        "Style de Réponse (affecte l'organisation)",
        options=list(RESPONSE_TYPES.keys()),
        format_func=lambda x: RESPONSE_TYPES[x],
        index=list(RESPONSE_TYPES.keys()).index(st.session_state.response_type),
        key="setting_response_type",
        on_change=lambda: update_preference('response_type'),
        help="Ceci définit la structure de l'aide fournie par l'IA (Étapes، مفهوم، أو إجابة نهائية)."
    )
    st.sidebar.markdown("---")


def main_app_ui():
    """Interface principale de l'application (pour les utilisateurs connectés)."""
    
    st.title("💡 Tuteur Mathématique Spécialisé (نظام المغرب)")
    st.markdown("---")

    st.markdown("أنا **مساعدك الذكي المتخصص**، جاهز لمساعدتك. يمكنك طرح سؤال أو **تحميل صورة** للتمرين.")

    col_upload, col_prompt = st.columns([1, 2])
    
    with col_upload:
        uploaded_file = st.file_uploader(
            "Optionnel : Téléchargez une photo (JPG / PNG، max 4 Mo).",
            type=["png", "jpg", "jpeg"],
            key="image_uploader"
        )
        
        if uploaded_file: 
            try:
                uploaded_file.seek(0)
                image = Image.open(BytesIO(uploaded_file.getvalue()))
                st.image(image, caption='Image téléchargée.', use_column_width=True)
            except Exception:
                st.error("Erreur lors du chargement de l'image.")
    
    with col_prompt:
        user_prompt = st.text_area(
            "Ajoutez votre question ou votre instruction ici.",
            height=250,
            key="prompt_input"
        )
        
        if st.button("Générer la Réponse Mathématique", use_container_width=True, type="primary"):
            if not user_prompt and not uploaded_file:
                st.warning("Veuillez entrer une question أو télécharger une image pour commencer.")
            else:
                with st.spinner('L\'IA analyse et prépare la réponse...'):
                    generated_text, sources = call_gemini_api(user_prompt, uploaded_file)
                
                st.subheader("✅ Réponse Générée :")
                
                if generated_text and "Limite de requêtes atteinte" not in generated_text and "Échec de l'API Gemini" not in generated_text:
                    st.write_stream(stream_text_simulation(generated_text))
                    
                    if sources:
                        st.subheader("🌐 Sources Citées :")
                        unique_sources = set((s['title'], s['uri']) for s in sources if s['uri'] and s['title'])
                        source_markdown = "\n".join([f"- [{title}]({uri})" for title, uri in unique_sources])
                        st.markdown(source_markdown)
                    else:
                        st.caption("Aucune source de recherche externe n'a été utilisée pour cette réponse.")
                else:
                    st.markdown(generated_text) # Affiche les messages d'erreur détaillés

    # Sidebar Status
    max_total_requests = MAX_REQUESTS + st.session_state.user_data.get('bonus_questions', 0)
    requests_left = max_total_requests - st.session_state.requests_today

    st.sidebar.header(f"Statut : {st.session_state.user_email}")
    st.sidebar.markdown(f"**Niveau Actuel:** {st.session_state.school_level}")
    st.sidebar.markdown(f"**Bonus Affiliation:** {st.session_state.user_data.get('bonus_questions', 0)} questions")
    
    # عرض لوحة الإدارة 
    if st.session_state.user_email == ADMIN_EMAIL.lower():
        admin_dashboard_ui()
    
    # 🌟 واجهة الإعدادات الجديدة 🌟
    settings_ui()


    if st.session_state.is_unlimited:
        status_message = "✅ **Utilisation Illimitée (VIP)**"
        color = "#28a745"
    else:
        status_message = f"Requêtes restantes aujourd'hui: **{requests_left}** / {max_total_requests}"
        color = "#007bff" if requests_left > 0 else "#dc3545"

    st.sidebar.markdown(f"""
    <div style='background-color:#e9ecef; padding:10px; border-radius:5px; text-align:center; border-left: 5px solid {color};'>
        <span style='font-weight: bold; color: {color};'>{status_message}</span>
    </div>
    """, unsafe_allow_html=True)
    
    st.sidebar.markdown("---")
    if st.sidebar.button("Déconnexion 🚪"):
        cookies[COOKIE_KEY_EMAIL] = ""
        cookies.save()
        st.session_state.auth_status = 'logged_out'
        st.session_state.should_rerun = True


# --- VII. Contrôle du Flux الرئيسي ---

# 1. Vérification du Cookie au démarrage
if st.session_state.auth_status == 'logged_out':
    remembered_email = cookies.get(COOKIE_KEY_EMAIL)
    if remembered_email:
        load_user_session(remembered_email) 
        
# 2. Affichage de l'UI
if st.session_state.auth_status == 'logged_out':
    auth_ui()
else:
    main_app_ui()

# 3. Traitement de l'auto-rerun 
if st.session_state.should_rerun:
    st.session_state.should_rerun = False
    st.rerun()

