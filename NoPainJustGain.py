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
# 2. MOTEUR D'EXTRACTION (MULTI-DOCUMENTS)
# ==========================================
def extraire_mois_bs(texte_bs):
    match = re.search(r"Période du \d{2}/(\d{2})", texte_bs, re.IGNORECASE)
    return match.group(1) if match else "05"

def lire_factures_bestt_batch(fichiers_factures, mois_cible):
    """
    Lit toutes les factures, extrait les noms des intérimaires 
    et leurs totaux facturés (Basé sur le format BESTT).
    """
    donnees_extraites = []
    
    for fichier in fichiers_factures:
        texte_complet = ""
        with pdfplumber.open(fichier) as pdf:
            for page in pdf.pages:
                texte_complet += page.extract_text() + "\n"
        
        # Regex pour trouver les lignes : "Total DELEU Denis (211h00mn) => 5 836,79 €"
        pattern = r"Total\s+([A-Za-z\s\-]+?)\s+\([\dhmn]+\)\s*=>\s*([\d\s]+,\d{2})\s*€"
        matches = re.findall(pattern, texte_complet)
        
        for match in matches:
            nom = match[0].strip()
            montant_str = match[1].replace(" ", "").replace(",", ".")
            donnees_extraites.append({
                "interimaire": nom,
                "total_facture": float(montant_str)
            })
            
    return donnees_extraites

def simuler_bs_pour_batch(donnees_factures):
    """
    Simulateur temporaire : Crée un faux BS cohérent pour chaque nom trouvé sur la facture.
    (La vraie extraction nécessitera les regex adaptées à votre logiciel de paie).
    """
    bs_batch = []
    for fact in donnees_factures:
        brut_estime = fact["total_facture"] / 1.95 # Estimation du brut via coef
        bs_batch.append({
            "interimaire": fact["interimaire"],
            "mois_cible": "05",
            "heures_normales": 151.67,
            "heures_sup": 10.0,
            "taux_horaire": 11.65,
            "primes_non_soumises": 50.00,
            "total_brut": brut_estime
        })
    return bs_batch

# ==========================================
# 3. MOTEUR DE CALCUL : CTT (x2) vs CDII
# ==========================================
def calculer_comparatif(bs_data, facture_data, is_pacte, taux_smic):
    const = get_constantes_pacte(is_pacte)
    
    hn = bs_data["heures_normales"]
    hs = bs_data["heures_sup"]
    brut_base = bs_data["total_brut"]
    facture = facture_data["total_facture"]
    
    heures_equiv = hn + (hs * MAJORATION_HS)
    montant_tepa = hs * const["tepa"]
    
    # -- CTT PROVISIONNÉ --
    smic_rgdu_ctt = taux_smic * heures_equiv * ICCP_TAUX
    ratio_prov = smic_rgdu_ctt / brut_base if brut_base > 0 else 0
    rgdu_prov = max(0, (const["t_rgdu"] / 0.6) * ((1.6 * ratio_prov) - 1)) * brut_base
    charges_nettes_prov = (brut_base * TAUX_CHARGES_BASE) - rgdu_prov - montant_tepa
    ifm_prov = brut_base * 0.10
    cp_prov = (brut_base + ifm_prov) * 0.10
    sequestre_total_prov = (ifm_prov + cp_prov) * (1 + TAUX_CHARGES_BASE)
    cout_total_prov = brut_base + bs_data["primes_non_soumises"] + charges_nettes_prov + sequestre_total_prov
    marge_prov = facture - cout_total_prov

    # -- CTT MENSUALISÉ --
    brut_mens = brut_base + ifm_prov + cp_prov
    ratio_mens = smic_rgdu_ctt / brut_mens if brut_mens > 0 else 0
    rgdu_mens = max(0, (const["t_rgdu"] / 0.6) * ((1.6 * ratio_mens) - 1)) * brut_mens
    charges_nettes_mens = (brut_mens * TAUX_CHARGES_BASE) - rgdu_mens - montant_tepa
    cout_total_mens = brut_mens + bs_data["primes_non_soumises"] + charges_nettes_mens
    marge_mens = facture - cout_total_mens

    # -- CDII --
    smic_rgdu_cdii = taux_smic * heures_equiv
    ratio_cdii = smic_rgdu_cdii / brut_base if brut_base > 0 else 0
    rgdu_cdii = max(0, (const["t_rgdu"] / 0.6) * ((1.6 * ratio_cdii) - 1)) * brut_base
    charges_nettes_cdii = (brut_base * (TAUX_CHARGES_BASE + TAUX_SURCOTISATION_CDII)) - rgdu_cdii - montant_tepa
    cp_cdii = brut_base * 0.10
    sequestre_total_cdii = cp_cdii * (1 + TAUX_CHARGES_BASE + TAUX_SURCOTISATION_CDII)
    cout_total_cdii = brut_base + bs_data["primes_non_soumises"] + charges_nettes_cdii + sequestre_total_cdii
    marge_cdii = facture - cout_total_cdii

    return {
        "Interimaire": bs_data["interimaire"],
        "Data": {
            "Lignes": ["Facturation", "Brut Soumis", "Allègement RGDU", "Coût Total", "MARGE NETTE"],
            "CTT (Provision)": [facture, brut_base, -rgdu_prov, cout_total_prov, marge_prov],
            "CTT (Mensualisé)": [facture, brut_mens, -rgdu_mens, cout_total_mens, marge_mens],
            "CDII": [facture, brut_base, -rgdu_cdii, cout_total_cdii, marge_cdii]
        }
    }

# ==========================================
# 4. INTERFACE UTILISATEUR & VUES (UI)
# ==========================================
st.set_page_config(page_title="NoPainJustGain", layout="wide")
st.title("🚀 NoPainJustGain : Audit de Marge ETT (Multi-Salariés)")

st.sidebar.header("Paramétrage Légal")
is_pacte = st.sidebar.checkbox("Loi Pacte (-20 salariés)", value=True)
taux_smic = st.sidebar.number_input("Taux horaire SMIC", value=12.31, step=0.10)

col1, col2 = st.columns(2)
with col1:
    fichiers_bs = st.file_uploader("📥 Déposer les Bulletins (PDF)", type=["pdf"], accept_multiple_files=True)
with col2:
    fichiers_factures = st.file_uploader("📥 Déposer les Factures BESTT (PDF)", type=["pdf"], accept_multiple_files=True)

if st.button("Lancer l'Audit Batch", type="primary"):
    if not fichiers_bs or not fichiers_factures:
        st.warning("Veuillez déposer des documents.")
    else:
        with st.spinner('Lecture complète des documents et rapprochement...'):
            # 1. Extraction en lot
            factures_batch = lire_factures_bestt_batch(fichiers_factures, "05")
            bs_batch = simuler_bs_pour_batch(factures_batch)
            
            # 2. Calcul pour chaque profil trouvé
            master_results = []
            for i in range(len(factures_batch)):
                res = calculer_comparatif(bs_batch[i], factures_batch[i], is_pacte, taux_smic)
                master_results.append(res)

        st.success(f"Audit terminé ! {len(master_results)} dossiers traités.")
        st.markdown("---")

        # 3. GRAPHISME ET COULEURS
        
        # A. Graphique Global de Synthèse
        st.subheader("📊 Synthèse Globale des Marges Nettes")
        
        graph_data = []
        for r in master_results:
            df_temp = pd.DataFrame(r["Data"]).set_index("Lignes")
            graph_data.append({"Intérimaire": r["Interimaire"], "Contrat": "CTT (Provision)", "Marge": df_temp.loc["MARGE NETTE", "CTT (Provision)"]})
            graph_data.append({"Intérimaire": r["Interimaire"], "Contrat": "CTT (Mensualisé)", "Marge": df_temp.loc["MARGE NETTE", "CTT (Mensualisé)"]})
            graph_data.append({"Intérimaire": r["Interimaire"], "Contrat": "CDII", "Marge": df_temp.loc["MARGE NETTE", "CDII"]})
            
        df_graph = pd.DataFrame(graph_data)
        fig = px.bar(df_graph, x="Intérimaire", y="Marge", color="Contrat", barmode="group",
                     color_discrete_map={"CTT (Provision)": "#1E88E5", "CTT (Mensualisé)": "#E53935", "CDII": "#43A047"})
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("---")
        st.subheader("📑 Détails Ligne à Ligne (Codes Couleurs)")
        
        # B. Boucle d'affichage pour chaque salarié avec formatage conditionnel
        for r in master_results:
            with st.expander(f"Dossier : {r['Interimaire']}", expanded=True):
                df = pd.DataFrame(r["Data"]).set_index("Lignes")
                
                # Fonction pour coloriser les lignes spécifiques
                def style_dataframe(row):
                    if row.name == "MARGE NETTE":
                        # Surligne la meilleure marge en vert, la pire en rouge
                        is_max = row == row.max()
                        is_min = row == row.min()
                        return ['background-color: #d4edda; color: #155724; font-weight: bold' if v 
                                else 'background-color: #f8d7da; color: #721c24' if m 
                                else 'font-weight: bold' for v, m in zip(is_max, is_min)]
                    
                    if row.name == "Allègement RGDU":
                        # Met en évidence la perte de RGDU (le chiffre le moins négatif est le pire)
                        is_worst_rgdu = row == row.max() 
                        return ['color: #721c24; font-weight: bold' if w else 'color: #155724' for w in is_worst_rgdu]
                    
                    return [''] * len(row)

                st.dataframe(
                    df.style.format("{:.2f} €").apply(style_dataframe, axis=1),
                    use_container_width=True
                )