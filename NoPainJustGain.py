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

# ==========================================
# 2. MOTEUR D'EXTRACTION FACTURES & BS
# ==========================================
def lire_factures_bestt_consolidees(fichiers_factures, mois_cible="05"):
    """
    Parcourt les factures, isole les lignes du mois cible, 
    et additionne STRICTEMENT le dernier montant de chaque ligne.
    """
    facturation_consolidee = {}
    
    for fichier in fichiers_factures:
        with pdfplumber.open(fichier) as pdf:
            for page in pdf.pages:
                lignes = page.extract_text().split('\n')
                interimaire_en_cours = None
                
                for ligne in lignes:
                    # 1. Détecte le nom de l'intérimaire
                    match_nom = re.search(r"^([A-Z\-]+\s[A-Za-z\-]+)\s+\(AGENT", ligne)
                    if match_nom:
                        interimaire_en_cours = match_nom.group(1).strip()
                        if interimaire_en_cours not in facturation_consolidee:
                            facturation_consolidee[interimaire_en_cours] = 0.0
                        continue
                    
                    # 2. Cherche le mois dans les parenthèses (ex: 04/05-08/05)
                    match_semaine = re.search(r"\([^)]*?(\d{2})/*\)", ligne)
                    if match_semaine and interimaire_en_cours:
                        mois_ligne = match_semaine.group(1)
                        
                        if mois_ligne == mois_cible:
                            # CORRECTION DU BUG : On prend TOUS les montants de la ligne...
                            montants = re.findall(r"([\d\s]+,\d{2})\s*€", ligne)
                            if montants:
                                # ... ET on ne garde que le DERNIER (le total, pas le taux unitaire)
                                montant_str = montants[-1].replace(" ", "").replace(",", ".")
                                facturation_consolidee[interimaire_en_cours] += float(montant_str)
                                
    donnees_extraites = []
    for nom, total in facturation_consolidee.items():
        if total > 0:
            donnees_extraites.append({"interimaire": nom, "total_facture": total})
            
    return donnees_extraites

def extraire_et_associer_bs(fichiers_bs, factures_data):
    """
    Lit tous les Bulletins de Salaire déposés, cherche à qui ils appartiennent,
    et extrait le Total Brut et les Heures de manière autonome.
    """
    # 1. On charge tout le texte des BS en mémoire
    textes_bs = []
    for f in fichiers_bs:
        texte = ""
        with pdfplumber.open(f) as pdf:
            for p in pdf.pages:
                texte += p.extract_text() + "\n"
        textes_bs.append(texte)
        
    resultats = []
    
    # 2. On croise chaque facture avec son BS
    for fact in factures_data:
        nom_facture = fact["interimaire"] # ex: "DELEU Denis"
        mots_nom = nom_facture.split()
        
        texte_cible = None
        # On cherche le PDF qui contient le Nom et le Prénom
        for texte in textes_bs:
            if mots_nom[0] in texte and mots_nom[-1] in texte:
                texte_cible = texte
                break
                
        if texte_cible:
            # Extraction du Brut (Cherche "TOTAL BRUT" ou "SALAIRE BRUT")
            matches_brut = re.findall(r"(?:TOTAL BRUT|SALAIRE BRUT).*?([\d\s]+,\d{2})", texte_cible, re.IGNORECASE)
            brut = float(matches_brut[-1].replace(" ", "").replace(",", ".")) if matches_brut else 0.0
            
            # Extraction des Heures
            matches_h = re.findall(r"(?:Heures normales|Base|Temps de travail).*?([\d\s]+,\d{2})", texte_cible, re.IGNORECASE)
            heures = float(matches_h[0].replace(" ", "").replace(",", ".")) if matches_h else 151.67
            
            bs_data = {
                "total_brut": brut,
                "heures_normales": heures,
                "heures_sup": 0.0,
                "heures_autres": 0.0,
                "primes_non_soumises": 0.0
            }
        else:
            # Si le BS n'a pas été trouvé / déposé
            bs_data = {"total_brut": 0.0, "heures_normales": 0.0, "heures_sup": 0.0, "heures_autres": 0.0, "primes_non_soumises": 0.0}
            
        resultats.append({"facture": fact, "bs": bs_data})
        
    return resultats

# ==========================================
# 3. MOTEUR DE CALCUL MÉTIER
# ==========================================
def calculer_comparatif(donnees, is_pacte, taux_smic):
    const = get_constantes_pacte(is_pacte)
    
    facture = donnees["facture"]["total_facture"]
    nom = donnees["facture"]["interimaire"]
    
    bs = donnees["bs"]
    brut_base = bs["total_brut"]
    
    # DÉTECTION DU COEFFICIENT COMMERCIAL
    coef_detecte = facture / brut_base if brut_base > 0 else 0
    
    heures_equiv = bs["heures_normales"] + bs["heures_autres"] + (bs["heures_sup"] * MAJORATION_HS)
    montant_tepa = bs["heures_sup"] * const["tepa"]
    
    # -- CTT PROVISIONNÉ --
    smic_rgdu_ctt = taux_smic * heures_equiv * ICCP_TAUX
    ratio_prov = smic_rgdu_ctt / brut_base if brut_base > 0 else 0
    c_rgdu_prov_calcul = (const["t_rgdu"] / 0.6) * ((1.6 * ratio_prov) - 1)
    c_rgdu_prov = min(const["t_rgdu"], max(0, c_rgdu_prov_calcul))
    rgdu_prov = c_rgdu_prov * brut_base
    
    charges_nettes_prov = (brut_base * TAUX_CHARGES_BASE) - rgdu_prov - montant_tepa
    ifm_prov = brut_base * 0.10
    cp_prov = (brut_base + ifm_prov) * 0.10
    sequestre_total_prov = (ifm_prov + cp_prov) * (1 + TAUX_CHARGES_BASE)
    cout_total_prov = brut_base + bs["primes_non_soumises"] + charges_nettes_prov + sequestre_total_prov
    marge_prov = facture - cout_total_prov

    # -- CTT MENSUALISÉ --
    brut_mens = brut_base + ifm_prov + cp_prov 
    ratio_mens = smic_rgdu_ctt / brut_mens if brut_mens > 0 else 0
    c_rgdu_mens_calcul = (const["t_rgdu"] / 0.6) * ((1.6 * ratio_mens) - 1)
    c_rgdu_mens = min(const["t_rgdu"], max(0, c_rgdu_mens_calcul))
    rgdu_mens = c_rgdu_mens * brut_mens
    
    charges_nettes_mens = (brut_mens * TAUX_CHARGES_BASE) - rgdu_mens - montant_tepa
    cout_total_mens = brut_mens + bs["primes_non_soumises"] + charges_nettes_mens
    marge_mens = facture - cout_total_mens

    # -- CDII --
    smic_rgdu_cdii = taux_smic * heures_equiv
    ratio_cdii = smic_rgdu_cdii / brut_base if brut_base > 0 else 0
    c_rgdu_cdii_calcul = (const["t_rgdu"] / 0.6) * ((1.6 * ratio_cdii) - 1)
    c_rgdu_cdii = min(const["t_rgdu"], max(0, c_rgdu_cdii_calcul))
    rgdu_cdii = c_rgdu_cdii * brut_base
    
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
        "Data": {
            "Lignes": ["1. Facturation HT", "2. Brut Soumis", "3. Allègement RGDU", "4. Séquestre ETT (IFM/CP)", "5. COÛT TOTAL", "6. MARGE NETTE"],
            "CTT (Provision)": [facture, brut_base, -rgdu_prov, sequestre_total_prov, cout_total_prov, marge_prov],
            "CTT (Mensualisé)": [facture, brut_mens, -rgdu_mens, 0.00, cout_total_mens, marge_mens],
            "CDII": [facture, brut_base, -rgdu_cdii, sequestre_total_cdii, cout_total_cdii, marge_cdii]
        }
    }

# ==========================================
# 4. INTERFACE UTILISATEUR
# ==========================================
st.set_page_config(page_title="NoPainJustGain", layout="wide")
st.title("🚀 NoPainJustGain : Audit de Marge Consolidé")

st.sidebar.header("Paramétrage Légal")
is_pacte = st.sidebar.checkbox("Loi Pacte (-20 salariés)", value=True)
taux_smic = st.sidebar.number_input("Taux horaire SMIC", value=12.02, step=0.01)

col1, col2 = st.columns(2)
with col1:
    fichiers_bs = st.file_uploader("📥 Déposer les Bulletins (PDF)", type=["pdf"], accept_multiple_files=True)
with col2:
    fichiers_factures = st.file_uploader("📥 Déposer les Factures (PDF)", type=["pdf"], accept_multiple_files=True)

if st.button("Lancer l'Audit Automatique", type="primary"):
    if not fichiers_factures or not fichiers_bs:
        st.warning("Veuillez déposer à la fois les Factures ET les Bulletins de Salaire.")
    else:
        with st.spinner('Consolidation des factures et lecture des BS en cours...'):
            factures_consolidees = lire_factures_bestt_consolidees(fichiers_factures, mois_cible="05")
            dossiers_complets = extraire_et_associer_bs(fichiers_bs, factures_consolidees)
            
            master_results = []
            for dossier in dossiers_complets:
                res = calculer_comparatif(dossier, is_pacte, taux_smic)
                master_results.append(res)

        st.success("Audit terminé ! Rapprochement Factures / BS effectué.")
        
        # ----------------------------------
        # VUE DÉTAILLÉE PAR SALARIÉ
        # ----------------------------------
        for r in master_results:
            # Sécurité visuelle si le BS n'a pas pu être lu correctement par l'OCR
            alerte_ocr = "⚠️ BRUT NON DÉTECTÉ SUR LE BS !" if r['BrutLu'] == 0.0 else ""
            
            with st.expander(f"Dossier : {r['Interimaire']} | Coef : {r['Coef']} | Heures : {r['Heures']}h {alerte_ocr}", expanded=True):
                if r['BrutLu'] == 0.0:
                    st.error("L'outil n'a pas réussi à lire le 'TOTAL BRUT' sur le PDF de ce bulletin. Le calcul de marge est donc faussé. Assurez-vous que le document déposé est bien lisible.")
                else:
                    if r['Coef'] < 1.80:
                        st.error(f"⚠️ Alerte : Coefficient très bas détecté ({r['Coef']})")
                    elif r['Coef'] >= 1.82:
                        st.success(f"✅ Coefficient commercial validé : {r['Coef']}")
                        
                df = pd.DataFrame(r["Data"]).set_index("Lignes")
                
                def style_dataframe(row):
                    if row.name == "6. MARGE NETTE":
                        is_max = row == row.max()
                        is_min = row == row.min()
                        return ['background-color: #d4edda; color: #155724; font-weight: bold' if v else 'background-color: #f8d7da; color: #721c24' if m else 'font-weight: bold' for v, m in zip(is_max, is_min)]
                    if row.name == "3. Allègement RGDU":
                        is_worst_rgdu = row == row.max() 
                        return ['color: #721c24; font-weight: bold' if w else 'color: #155724' for w in is_worst_rgdu]
                    return [''] * len(row)

                st.dataframe(df.style.format("{:.2f} €").apply(style_dataframe, axis=1), use_container_width=True)