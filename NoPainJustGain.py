import streamlit as st
import pandas as pd
import pdfplumber
import re
import plotly.express as px

# ==========================================
# 1. PARAMÈTRES ET CONSTANTES MÉTIER
# ==========================================
MAJORATION_HS = 1.25
ICCP_TAUX = 1.10
TAUX_CHARGES_BASE = 0.45
TAUX_SURCOTISATION_CDII = 0.035

def get_constantes_pacte(is_pacte):
    if is_pacte:
        return {"fnal": 0.0010, "tepa": 1.50, "t_rgdu": 0.3191}
    return {"fnal": 0.0050, "tepa": 0.50, "t_rgdu": 0.3231}

MOIS_LABELS = {
    "01": "Janvier", "02": "Février", "03": "Mars", "04": "Avril",
    "05": "Mai", "06": "Juin", "07": "Juillet", "08": "Août",
    "09": "Septembre", "10": "Octobre", "11": "Novembre", "12": "Décembre",
}

# ==========================================
# 2. OUTILS DE PARSING GÉNÉRIQUES
# ==========================================
# Nombre au format FR : 1 234,56 / 1234,56 / 3 170,108 (2 à 3 décimales ; les colonnes
# IFM/CP des bulletins affichent 3 décimales, ce qui casserait un parsing figé à 2 décimales).
_MONTANT_FR = r"\d{1,3}(?:[ \u00a0\u202f.]\d{3})*,\d{2,3}|\d+,\d{2,3}"

def parse_montant_fr(s):
    """Convertit un montant FR ('3 170,11' / '3.170,11' / '2393,78') en float."""
    if s is None:
        return 0.0
    s = s.strip()
    for ch in ("\u202f", "\u00a0", " "):
        s = s.replace(ch, "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")  # virgule décimale -> points = milliers
    try:
        return float(s)
    except ValueError:
        return 0.0

def _tous_les_montants(ligne):
    return [parse_montant_fr(m) for m in re.findall(_MONTANT_FR, ligne)]

# ==========================================
# 3. MOTEUR D'EXTRACTION FACTURES (BESTT)
# ==========================================
# Nom d'intérimaire en début de ligne suivi d'un intitulé de poste entre parenthèses.
# Accepte les accents et n'importe quel intitulé (pas seulement "AGENT").
_RE_NOM = re.compile(r"^([A-ZÀ-Ÿ][A-ZÀ-Ÿ'\-]+(?:\s+[A-Za-zÀ-ÿ'\-]+)+?)\s*\(", re.UNICODE)
# Toutes les dates jj/mm (avec éventuellement /aa ou /aaaa)
_RE_DATES = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/\d{2,4})?\b")

def mois_de_la_ligne(ligne):
    """
    Renvoie (mois_attribution, liste_mois_trouves).
    Règle : mois d'attribution d'une semaine = mois de la DATE DE FIN
    (dernière date jj/mm de la ligne), reflétant la prestation facturée.
    """
    dates = _RE_DATES.findall(ligne)
    if not dates:
        return None, []
    mois_trouves = [mm.zfill(2) for (_jj, mm) in dates]
    return mois_trouves[-1], mois_trouves

def est_ligne_prestation(ligne):
    """Ligne de prestation = contient au moins une date (et souvent un repère 'Sem.')."""
    return bool(_RE_DATES.search(ligne))

def parser_lignes_facture(lignes, nom_fichier, mois_cible, consolidation):
    """Parse les lignes d'une page de facture et alimente `consolidation` (dict mutable)."""
    interimaire = None
    for ligne in lignes:
        if not ligne or not ligne.strip():
            continue

        m_nom = _RE_NOM.search(ligne)
        if m_nom and not _RE_DATES.search(ligne):
            interimaire = re.sub(r"\s+", " ", m_nom.group(1)).strip()
            consolidation.setdefault(interimaire, {"total": 0.0, "historique_lignes": []})
            continue

        if interimaire and est_ligne_prestation(ligne):
            mois_attr, mois_trouves = mois_de_la_ligne(ligne)
            if mois_attr is None:
                continue
            retenue = (mois_attr == mois_cible)
            montants = _tous_les_montants(ligne)
            montant = montants[-1] if montants else 0.0
            statut = "✅ RETENUE" if retenue else f"⏭️ ignorée (mois {mois_attr})"
            consolidation[interimaire]["historique_lignes"].append({
                "fichier": nom_fichier, "ligne": ligne.strip(),
                "mois_attribution": mois_attr, "mois_trouves": mois_trouves,
                "montant": montant, "retenue": retenue, "statut": statut,
            })
            if retenue:
                consolidation[interimaire]["total"] += montant

def lire_factures_bestt_consolidees(fichiers_factures, mois_cible="05"):
    consolidation = {}
    for fichier in fichiers_factures:
        with pdfplumber.open(fichier) as pdf:
            for page in pdf.pages:
                texte = page.extract_text() or ""
                parser_lignes_facture(texte.split("\n"), fichier.name, mois_cible, consolidation)

    donnees = []
    for nom, data in consolidation.items():
        if data["total"] > 0:
            donnees.append({
                "interimaire": nom,
                "total_facture": round(data["total"], 2),
                "lignes_retenues": data["historique_lignes"],
            })
    return donnees

# ==========================================
# 4. MOTEUR D'EXTRACTION BULLETINS DE SALAIRE
# ==========================================
_LABELS_HEURES = [
    r"Heures?\s+normales?", r"Temps\s+de\s+travail", r"Salaire\s+de\s+base", r"\bBase\b",
]

def grouper_lignes_depuis_mots(mots, tol=3.0):
    """
    Reconstruit les lignes VISUELLES d'une page à partir des mots positionnés
    (pdfplumber extract_words). Indispensable pour les bulletins en colonnes :
    le libellé 'SB' et sa valeur, à droite, se retrouvent sur la même ligne,
    et le montant '2 393,78' (fragmenté par l'espace des milliers) est reformé.
    """
    rows = []
    for w in sorted(mots, key=lambda m: (m["top"], m["x0"])):
        placed = False
        for r in rows:
            if abs(r["top"] - w["top"]) <= tol:
                r["mots"].append(w)
                r["top"] = (r["top"] * (len(r["mots"]) - 1) + w["top"]) / len(r["mots"])
                placed = True
                break
        if not placed:
            rows.append({"top": w["top"], "mots": [w]})
    lignes = []
    for r in sorted(rows, key=lambda r: r["top"]):
        r["mots"].sort(key=lambda m: m["x0"])
        lignes.append(" ".join(m["text"] for m in r["mots"]))
    return lignes

# SB (colonne « = BRUT ») = base + IFM(10%) + CP(10% sur base+IFM) = 1,21 × base.
# On dérive donc la base soumise à partir du SB lu.
RATIO_SB_BASE = 1.21
_RE_SB = re.compile(r"(?<![A-Za-zÀ-ÿ0-9])S\.?\s?B\.?(?![A-Za-zÀ-ÿ0-9])")

def brut_depuis_rows(rows):
    """
    Extrait le brut social depuis des lignes visuelles reconstruites.
    Structure BESTT/paie : ligne TOTAUX du bloc « SOUMISES À COTISATIONS » avec les
    colonnes MONTANT | +IFM | +CP | = BRUT, et le code « SB » en marge de DROITE,
    juste APRÈS la valeur du brut.
    Retourne (sb, base, methode, ligne_source) ; (None, None, None, None) si rien
    (à distinguer d'un brut réellement égal à 0).
    """
    def montants(s):
        return [parse_montant_fr(x) for x in re.findall(_MONTANT_FR, s)]

    # 1) Code « SB » : le BRUT est le DERNIER montant AVANT le code SB (marge de droite).
    for r in rows:
        m = _RE_SB.search(r)
        if m:
            avant = montants(r[:m.start()])
            if avant:
                sb = avant[-1]
                base = sb / RATIO_SB_BASE
                # Si les 4 colonnes MONTANT/IFM/CP/BRUT sont présentes et cohérentes,
                # on prend la base exacte (colonne MONTANT) plutôt que la dérivation.
                if len(avant) >= 4:
                    b, i, c, br = avant[-4], avant[-3], avant[-2], avant[-1]
                    if br > 0 and abs((b + i + c) - br) <= max(0.05, 0.01 * br):
                        base, sb = b, br
                return sb, base, "SB (colonne « = BRUT »)", r

    # 2) Libellés explicites -> montant = SB, base dérivée
    libelles = [
        (r"salaire\s+brut(?:\s+imposable)?", "Salaire brut"),
        (r"brut\s+social", "Brut social"),
        (r"total\s+brut", "Total brut"),
        (r"brut\s+total", "Brut total"),
        (r"r[ée]mun[ée]ration\s+brute", "Rémunération brute"),
        (r"brut\s+fiscal", "Brut fiscal"),
    ]
    for pattern, name in libelles:
        rx = re.compile(pattern, re.IGNORECASE)
        for r in rows:
            if rx.search(r):
                ms = montants(r)
                if ms:
                    sb = ms[-1]
                    return sb, sb / RATIO_SB_BASE, name, r

    # 3) 'brut' seul -> dernier montant
    for r in rows:
        if re.search(r"\bbrut\b", r, re.IGNORECASE):
            ms = montants(r)
            if ms:
                sb = ms[-1]
                return sb, sb / RATIO_SB_BASE, "Brut", r

    return None, None, None, None

def extraire_brut(texte, mots=None):
    """
    Retourne (sb, base, methode, ligne_source, lignes_visuelles).
    sb = brut social (colonne = BRUT, inclut IFM/CP) ; base = sb / 1,21 (base soumise).
    sb = None si aucune ligne de brut n'est identifiée (≠ brut = 0).
    """
    if mots:
        rows = grouper_lignes_depuis_mots(mots)
        sb, base, methode, ligne = brut_depuis_rows(rows)
        if sb is not None:
            return sb, base, methode, ligne, rows
        sb2, base2, methode2, ligne2 = brut_depuis_rows(texte.split("\n"))
        return sb2, base2, methode2, ligne2, rows
    rows = texte.split("\n")
    sb, base, methode, ligne = brut_depuis_rows(rows)
    return sb, base, methode, ligne, rows

def extraire_heures(texte, rows=None, defaut=151.67):
    """Heures totales travaillées : priorité à la ligne TOTAUX « HEURES : X h »."""
    candidats = rows if rows else texte.split("\n")
    # 1) Total explicite « HEURES : 199,15 h » (ligne TOTAUX du bloc soumis)
    for r in candidats:
        m = re.search(r"HEURES\s*:?\s*(\d{1,3}(?:[.,]\d{1,2})?)\s*h", r, re.IGNORECASE)
        if m:
            g = m.group(1)
            val = parse_montant_fr(g) if "," in g else float(g)
            if 1 <= val <= 400:
                return val
    # 2) Repli : libellés d'heures classiques
    for label in _LABELS_HEURES:
        rx = re.compile(label, re.IGNORECASE)
        for ligne in candidats:
            if rx.search(ligne):
                for mm in re.findall(r"\d{1,3}(?:[.,]\d{1,2})?", ligne):
                    val = parse_montant_fr(mm) if "," in mm else float(mm.replace(",", "."))
                    if 1 <= val <= 400:
                        return val
    return defaut

def _lire_bs(fichiers_bs):
    """Lit chaque BS : texte (pour l'association nom) + mots positionnés (pour le brut)."""
    docs = []
    for f in fichiers_bs:
        texte, mots = "", []
        with pdfplumber.open(f) as pdf:
            for i, p in enumerate(pdf.pages):
                texte += (p.extract_text() or "") + "\n"
                for w in (p.extract_words() or []):
                    # décalage vertical par page pour ne pas fusionner des lignes de pages différentes
                    mots.append({"text": w["text"], "x0": w["x0"], "x1": w["x1"],
                                 "top": w["top"] + i * 100000, "bottom": w["bottom"] + i * 100000})
        docs.append({"name": f.name, "texte": texte, "mots": mots})
    return docs

def extraire_et_associer_bs(fichiers_bs, factures_data):
    """Associe les données de paie exactes aux factures consolidées."""
    docs = _lire_bs(fichiers_bs)

    resultats = []
    for fact in factures_data:
        nom_facture = fact["interimaire"]
        mots_nom = nom_facture.split()
        nom_famille = mots_nom[0]           # NOM (souvent en majuscules, distinctif)
        prenom = mots_nom[-1] if len(mots_nom) > 1 else ""

        doc_cible = None
        # 1) match strict NOM + Prénom, 2) fallback sur le NOM seul
        for d in docs:
            if nom_famille in d["texte"] and (not prenom or prenom in d["texte"]):
                doc_cible = d
                break
        if doc_cible is None:
            for d in docs:
                if nom_famille in d["texte"]:
                    doc_cible = d
                    break

        if doc_cible:
            sb, base, methode_brut, ligne_brut, rows_diag = extraire_brut(
                doc_cible["texte"], doc_cible["mots"])
            heures = extraire_heures(doc_cible["texte"], rows_diag)
            trouve = sb is not None
            bs_data = {
                "brut_sb": sb if trouve else 0.0,          # brut social affiché (inclut IFM/CP)
                "total_brut": base if trouve else 0.0,     # base soumise (= SB/1,21) pour le calcul
                "brut_trouve": trouve,                     # distingue "introuvable" de "= 0,00"
                "heures_normales": heures,
                "heures_sup": 0.0, "heures_autres": 0.0, "primes_non_soumises": 0.0,
                "fichier_bs": doc_cible["name"], "label_brut": methode_brut,
                "ligne_brut": ligne_brut,
                "lignes_diag": rows_diag,                  # pour le panneau diagnostic
            }
        else:
            bs_data = {
                "brut_sb": 0.0, "total_brut": 0.0, "brut_trouve": False,
                "heures_normales": 0.0, "heures_sup": 0.0, "heures_autres": 0.0,
                "primes_non_soumises": 0.0,
                "fichier_bs": None, "label_brut": None,
                "ligne_brut": None, "lignes_diag": [],
            }
        resultats.append({"facture": fact, "bs": bs_data})
    return resultats

# ==========================================
# 5. MOTEUR DE CALCUL MÉTIER
# ==========================================
def calculer_comparatif(donnees, is_pacte, taux_smic):
    const = get_constantes_pacte(is_pacte)

    facture = donnees["facture"]["total_facture"]
    lignes_facture = donnees["facture"]["lignes_retenues"]
    nom = donnees["facture"]["interimaire"]

    bs = donnees["bs"]
    brut_base = bs["total_brut"]                    # base soumise (= SB / 1,21)
    brut_sb = bs.get("brut_sb", brut_base * RATIO_SB_BASE)  # brut social affiché (inclut IFM/CP)
    coef_detecte = facture / brut_base if brut_base > 0 else 0

    heures_equiv = bs["heures_normales"] + bs["heures_autres"] + (bs["heures_sup"] * MAJORATION_HS)
    montant_tepa = bs["heures_sup"] * const["tepa"]

    def coef_rgdu(smic_ref, brut_ref):
        if brut_ref <= 0:
            return 0.0
        ratio = smic_ref / brut_ref
        c = (const["t_rgdu"] / 0.6) * ((1.6 * ratio) - 1)
        c = min(const["t_rgdu"], max(0.0, c))
        return c * brut_ref

    # -- CTT PROVISIONNÉ -- (SMIC majoré ICCP +10%)
    smic_rgdu_ctt = taux_smic * heures_equiv * ICCP_TAUX
    rgdu_prov = coef_rgdu(smic_rgdu_ctt, brut_base)
    charges_nettes_prov = (brut_base * TAUX_CHARGES_BASE) - rgdu_prov - montant_tepa
    ifm_prov = brut_base * 0.10
    cp_prov = (brut_base + ifm_prov) * 0.10
    sequestre_total_prov = (ifm_prov + cp_prov) * (1 + TAUX_CHARGES_BASE)
    cout_total_prov = brut_base + bs["primes_non_soumises"] + charges_nettes_prov + sequestre_total_prov
    marge_prov = facture - cout_total_prov

    # -- CTT MENSUALISÉ -- (IFM/CP intégrés au brut soumis)
    brut_mens = brut_base + ifm_prov + cp_prov
    rgdu_mens = coef_rgdu(smic_rgdu_ctt, brut_mens)
    charges_nettes_mens = (brut_mens * TAUX_CHARGES_BASE) - rgdu_mens - montant_tepa
    cout_total_mens = brut_mens + bs["primes_non_soumises"] + charges_nettes_mens
    marge_mens = facture - cout_total_mens

    # -- CDII -- (pas d'IFM, pas d'ICCP, surcotisation +3.5%)
    smic_rgdu_cdii = taux_smic * heures_equiv
    rgdu_cdii = coef_rgdu(smic_rgdu_cdii, brut_base)
    charges_nettes_cdii = (brut_base * (TAUX_CHARGES_BASE + TAUX_SURCOTISATION_CDII)) - rgdu_cdii - montant_tepa
    cp_cdii = brut_base * 0.10
    sequestre_total_cdii = cp_cdii * (1 + TAUX_CHARGES_BASE + TAUX_SURCOTISATION_CDII)
    cout_total_cdii = brut_base + bs["primes_non_soumises"] + charges_nettes_cdii + sequestre_total_cdii
    marge_cdii = facture - cout_total_cdii

    return {
        "Interimaire": nom,
        "Heures": round(heures_equiv, 2),
        "Coef": round(coef_detecte, 2),
        "BrutLu": brut_base,
        "BrutSB": brut_sb,
        "BrutTrouve": bs.get("brut_trouve", brut_base > 0),
        "FichierBS": bs.get("fichier_bs"),
        "LabelBrut": bs.get("label_brut"),
        "LigneBrut": bs.get("ligne_brut"),
        "LignesDiag": bs.get("lignes_diag", []),
        "LignesRetenues": lignes_facture,
        "Marges": {
            "CTT (Provision)": round(marge_prov, 2),
            "CTT (Mensualisé)": round(marge_mens, 2),
            "CDII": round(marge_cdii, 2),
        },
        "Data": {
            "Lignes": ["1. Facturation HT", "2. Brut Soumis", "3. Allègement RGDU",
                       "4. Séquestre ETT (IFM/CP)", "5. COÛT TOTAL", "6. MARGE NETTE"],
            "CTT (Provision)": [facture, brut_base, -rgdu_prov, sequestre_total_prov, cout_total_prov, marge_prov],
            "CTT (Mensualisé)": [facture, brut_mens, -rgdu_mens, 0.00, cout_total_mens, marge_mens],
            "CDII": [facture, brut_base, -rgdu_cdii, sequestre_total_cdii, cout_total_cdii, marge_cdii],
        },
    }

def recommander_statut(marges):
    """Détermine le statut optimal + le signal d'intégration IFM/CP en cours de mois."""
    best = max(marges, key=marges.get)
    delta_integration = marges["CTT (Mensualisé)"] - marges["CTT (Provision)"]
    if delta_integration > 0:
        signal_ifm = ("✅ Intégrer les IFM/CP dès maintenant : la mensualisation améliore "
                      f"la marge de {delta_integration:+.2f} € (allègement RGDU plus favorable).")
    else:
        signal_ifm = ("🔒 Laisser les IFM/CP en séquestre : la mensualisation coûterait "
                      f"{delta_integration:+.2f} € de marge tant que la mission n'est pas terminée.")
    return best, delta_integration, signal_ifm

# ==========================================
# 6. INTERFACE UTILISATEUR
# ==========================================
st.set_page_config(page_title="NoPainJustGain", layout="wide")
st.title("🚀 NoPainJustGain : Audit de Marge Consolidé")

st.sidebar.header("Paramétrage Légal")
is_pacte = st.sidebar.checkbox("Loi Pacte (-20 salariés)", value=True)
taux_smic = st.sidebar.number_input("Taux horaire SMIC", value=12.02, step=0.01)
mois_cible = st.sidebar.selectbox(
    "Mois cible (mois du BS)",
    options=list(MOIS_LABELS.keys()),
    format_func=lambda k: f"{k} — {MOIS_LABELS[k]}",
    index=4,  # Mai par défaut
)

col1, col2 = st.columns(2)
with col1:
    fichiers_bs = st.file_uploader("📥 Déposer les Bulletins (PDF)", type=["pdf"], accept_multiple_files=True)
with col2:
    fichiers_factures = st.file_uploader("📥 Déposer les Factures (PDF)", type=["pdf"], accept_multiple_files=True)

if st.button("Lancer l'Audit Automatique", type="primary"):
    if not fichiers_factures or not fichiers_bs:
        st.warning("Veuillez déposer à la fois les Factures ET les Bulletins de Salaire.")
    else:
        with st.spinner("Consolidation des factures et lecture des BS en cours..."):
            factures_consolidees = lire_factures_bestt_consolidees(fichiers_factures, mois_cible=mois_cible)
            dossiers_complets = extraire_et_associer_bs(fichiers_bs, factures_consolidees)

        if not dossiers_complets:
            st.error("Aucun intérimaire n'a pu être consolidé sur le mois cible. "
                     "Vérifiez le mois sélectionné et le format des factures.")
            st.stop()

        # On mémorise les données BRUTES extraites (brut auto inclus) et on réinitialise
        # les corrections manuelles éventuelles d'un audit précédent.
        st.session_state["dossiers_audit"] = dossiers_complets
        st.session_state["mois_audit"] = mois_cible
        for k in [k for k in st.session_state.keys() if k.startswith("brut_")]:
            del st.session_state[k]

# ==========================================================
# RENDU (hors bouton) : recalcul à chaque modification du brut
# ==========================================================
if "dossiers_audit" in st.session_state:
    dossiers = st.session_state["dossiers_audit"]
    mois_audit = st.session_state.get("mois_audit", mois_cible)

    st.success(f"Audit — mois {mois_audit} ({MOIS_LABELS[mois_audit]}). "
               "Rapprochement Factures / BS effectué.")

    # ----------------------------------------------------------
    # VÉRIFICATION / CORRECTION MANUELLE DU BRUT (garde-fou)
    # ----------------------------------------------------------
    st.header("🧾 Vérification du brut social (SB)")
    st.caption("Le brut social **SB** (colonne « = BRUT » du bloc soumis à cotisations, IFM + CP inclus) "
               "est lu automatiquement, puis la **base soumise** est dérivée (SB ÷ 1,21) pour les calculs. "
               "Le SB est modifiable : tout se recalcule aussitôt. "
               "Un SB détecté à 0,00 € n'est pas une erreur de lecture mais une anomalie de paie signalée.")

    sb_effectif = {}
    for d in dossiers:
        nom = d["facture"]["interimaire"]
        bs = d["bs"]
        auto_sb = float(bs.get("brut_sb", 0.0))
        trouve = bs.get("brut_trouve", auto_sb > 0)
        methode = bs.get("label_brut")
        fichier = bs.get("fichier_bs") or "—"
        ligne_brut = bs.get("ligne_brut")

        key = f"brut_{nom}"
        if key not in st.session_state:
            st.session_state[key] = auto_sb  # valeur initiale = SB détecté

        c_nom, c_src, c_val = st.columns([2, 3, 2])
        c_nom.markdown(f"**{nom}**")
        if not trouve:
            c_src.caption(f"BS : {fichier} · 🔴 **SB introuvable** — à saisir à droite")
        elif auto_sb == 0.0:
            c_src.caption(f"BS : {fichier} · ⚠️ SB détecté = 0,00 € (anomalie de paie)")
        else:
            c_src.caption(f"BS : {fichier} · ✅ {methode} → SB {auto_sb:.2f} € "
                          f"(base ≈ {auto_sb / RATIO_SB_BASE:.2f} €)")
            if ligne_brut:
                c_src.caption(f"↳ ligne : `{ligne_brut[:90]}`")
        sb_val = c_val.number_input("Brut social SB (€)", min_value=0.0, step=10.0,
                                    key=key, label_visibility="collapsed")
        sb_effectif[nom] = sb_val

    # Panneau diagnostic global : montre où le parser lit sur chaque BS
    with st.expander("🔬 Diagnostic de lecture des BS (pour identifier la bonne ligne de brut)",
                     expanded=False):
        st.caption("Pour chaque bulletin, voici les lignes reconstruites et les montants repérés. "
                   "Repérez la valeur exacte du brut : si le parser ne tombe pas dessus, "
                   "corrigez à la main ci-dessus et indiquez-moi le libellé exact de cette ligne "
                   "pour que je fiabilise la détection automatique.")
        for d in dossiers:
            nom = d["facture"]["interimaire"]
            bs = d["bs"]
            with st.expander(f"BS de {nom} — {bs.get('fichier_bs') or '—'}", expanded=False):
                lignes_diag = bs.get("lignes_diag") or []
                if not lignes_diag:
                    st.caption("Aucune ligne exploitable (PDF scanné/image ? → OCR requis).")
                choisie = bs.get("ligne_brut")
                for ligne in lignes_diag:
                    montants = re.findall(_MONTANT_FR, ligne)
                    if not montants:
                        continue
                    marqueur = "➡️ **[LIGNE RETENUE]** " if ligne == choisie else ""
                    st.markdown(f"{marqueur}`{ligne}`  —  montants : {', '.join(montants)}")

    # ----------------------------------------------------------
    # CALCUL (avec brut éventuellement corrigé, sans muter l'auto)
    # ----------------------------------------------------------
    master_results = []
    for d in dossiers:
        nom = d["facture"]["interimaire"]
        sb = sb_effectif[nom]
        d_calc = {"facture": d["facture"],
                  "bs": {**d["bs"],
                         "brut_sb": sb,
                         "total_brut": sb / RATIO_SB_BASE}}  # base soumise = SB / 1,21
        master_results.append(calculer_comparatif(d_calc, is_pacte, taux_smic))

    # ----------------------------------------------------------
    # SYNTHÈSE GRAPHIQUE : choix de contrat & intégration IFM/CP
    # ----------------------------------------------------------
    st.header("📊 Synthèse : choix du contrat optimal")

    rows = []
    for r in master_results:
        for statut, marge in r["Marges"].items():
            rows.append({"Intérimaire": r["Interimaire"], "Statut": statut, "Marge (€)": marge})
    df_graph = pd.DataFrame(rows)

    couleurs = {"CTT (Provision)": "#1f77b4", "CTT (Mensualisé)": "#ff7f0e", "CDII": "#2ca02c"}
    fig = px.bar(
        df_graph, x="Intérimaire", y="Marge (€)", color="Statut",
        barmode="group", color_discrete_map=couleurs, text_auto=".0f",
        title="Marge nette comparée par statut (le plus haut = optimal)",
    )
    fig.update_layout(legend_title_text="Statut", yaxis_title="Marge nette (€)",
                      xaxis_title="", uniformtext_minsize=8, uniformtext_mode="hide")
    fig.add_hline(y=0, line_dash="dash", line_color="grey")
    st.plotly_chart(fig, use_container_width=True)

    # Totaux globaux par statut
    totaux = df_graph.groupby("Statut")["Marge (€)"].sum()
    c1, c2, c3 = st.columns(3)
    for col, statut in zip((c1, c2, c3), ["CTT (Provision)", "CTT (Mensualisé)", "CDII"]):
        col.metric(f"Total {statut}", f"{totaux.get(statut, 0):,.0f} €".replace(",", " "))

    st.subheader("🧭 Recommandations par intérimaire")
    reco_rows = []
    for r in master_results:
        best, delta, signal = recommander_statut(r["Marges"])
        reco_rows.append({
            "Intérimaire": r["Interimaire"],
            "Contrat optimal": best,
            "Marge optimale (€)": r["Marges"][best],
            "Δ Mensualisation IFM/CP (€)": round(delta, 2),
            "Décision IFM/CP": "Intégrer" if delta > 0 else "Séquestrer",
        })
    st.dataframe(pd.DataFrame(reco_rows), use_container_width=True, hide_index=True)
    st.caption("💡 « Δ Mensualisation » = marge CTT Mensualisé − marge CTT Provision. "
               "Positif → intégrer les IFM/CP en cours de mois est gagnant ; "
               "négatif → mieux vaut les laisser en séquestre tant que la mission n'est pas soldée.")

    # ----------------------------------------------------------
    # VUE DÉTAILLÉE PAR SALARIÉ
    # ----------------------------------------------------------
    st.header("🔎 Détail par dossier")

    def style_dataframe(row):
        if row.name == "6. MARGE NETTE":
            is_max, is_min = row == row.max(), row == row.min()
            return [
                "background-color: #d4edda; color: #155724; font-weight: bold" if v
                else "background-color: #f8d7da; color: #721c24" if m
                else "font-weight: bold"
                for v, m in zip(is_max, is_min)
            ]
        if row.name == "3. Allègement RGDU":
            is_worst = row == row.max()  # RGDU le moins négatif = le moins d'allègement
            return ["color: #721c24; font-weight: bold" if w else "color: #155724" for w in is_worst]
        return [""] * len(row)

    for r in master_results:
        brut0 = r["BrutLu"] == 0.0
        trouve = r.get("BrutTrouve", not brut0)
        if brut0 and not trouve:
            badge = "🔴 BRUT INTROUVABLE"
        elif brut0:
            badge = "⚠️ BRUT = 0 (anomalie)"
        elif r["Coef"] and r["Coef"] > 3.0:
            badge = "⚠️ COEF ANORMAL"
        else:
            badge = ""
        best, delta, signal = recommander_statut(r["Marges"])

        with st.expander(
            f"Dossier : {r['Interimaire']} | Coef : {r['Coef']} | Heures : {r['Heures']}h "
            f"| Optimal : {best} {badge}",
            expanded=True,
        ):
            if brut0 and not trouve:
                st.error("🔴 Brut introuvable sur ce bulletin : la lecture automatique a échoué. "
                         "Saisissez le brut dans « Vérification du brut social » ci-dessus "
                         "(voir aussi le panneau 🔬 Diagnostic pour repérer la bonne ligne).")
            elif brut0:
                st.warning("⚠️ Brut détecté = 0,00 € : anomalie de paie (intérimaire facturé mais "
                           "non rémunéré sur le mois). Coefficient et marges non calculables en l'état.")
            else:
                st.caption(f"Brut social (SB) : **{r['BrutSB']:.2f} €** · base soumise "
                           f"{r['BrutLu']:.2f} € (SB ÷ 1,21) · source : {r['FichierBS']} · {r['LabelBrut']}")
                if r["Coef"] > 3.0:
                    st.error(f"⚠️ Coefficient anormalement élevé ({r['Coef']}) — probable "
                             "décalage facture/BS (période, temps partiel, brut erroné). À vérifier.")
                elif r["Coef"] < 1.82:
                    st.error(f"⚠️ Coefficient bas détecté ({r['Coef']}) — seuil conseillé ≥ 1,82")
                else:
                    st.success(f"✅ Coefficient commercial validé : {r['Coef']}")
                st.info(signal)

            # --- AFFICHAGE OPTIONNEL DES LIGNES DE FACTURE (déployé au clic) ---
            lignes = r["LignesRetenues"]
            nb_retenues = sum(1 for l in lignes if l["retenue"])
            with st.expander(
                f"🔍 Voir le détail de la consolidation "
                f"({nb_retenues} ligne(s) retenue(s) / {len(lignes)} analysée(s)) — cliquer pour déployer",
                expanded=False,
            ):
                montrer_ignorees = st.checkbox(
                    "Afficher aussi les lignes ignorées (autres mois)",
                    value=False, key=f"chk_{r['Interimaire']}",
                )
                for l in lignes:
                    if l["retenue"] or montrer_ignorees:
                        st.caption(f"{l['statut']} · [{l['fichier']}] "
                                   f"{l['montant']:.2f} € — {l['ligne']}")

            # --- TABLEAU COMPARATIF ---
            df = pd.DataFrame(r["Data"]).set_index("Lignes")
            st.dataframe(
                df.style.format("{:.2f} €").apply(style_dataframe, axis=1),
                use_container_width=True,
            )
