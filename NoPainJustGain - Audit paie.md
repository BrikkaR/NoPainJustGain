---
activité: TALENTIS
tags: [talentis, marges, outil, paie, rgdu, cdii]
type: projet
maj: 2026-11-17
---
# NoPainJustGain — Audit paie (outil Streamlit)

Outil d'audit de marge et de contrôle de gestion pour l'agence. Il lit les **factures clients** (PDF BESTT / Pixid) et les **bulletins de salaire**, rapproche facturation et coût réel de paie, recalcule la rentabilité exacte et compare les marges selon **trois statuts de contrat**. Déployé sur Streamlit.

## À quoi il sert
- Vérifier, mois par mois et par intérimaire, que la **facturation couvre bien le coût** chargé.
- Comparer **CTT provisionné / CTT mensualisé / CDII** pour choisir le contrat le plus rentable.
- Détecter les **anomalies** : coefficient commercial, intérimaires payés non facturés, prestations de management facturées à part, factures de fin de mois manquantes.

## Les trois statuts comparés (pour une même facture)
- **CTT (Provision)** — IFM + CP séquestrés, versés en fin de mission. **N'apporte PAS de marge** : la sur-déduction RGDU de cours de mois est reprise par la régularisation. Seul avantage = **trésorerie**.
- **CTT (Mensualisé)** — IFM + CP intégrés au brut chaque mois. C'est la **marge CTT réelle** (= Provision après régularisation).
- **CDII** — pas d'IFM, CP payées, surcotisation patronale +3,5 % (AKTO/FSPI), pas de majoration caisse CP.

## Conclusion métier (validée)
- Le **CDII est le plus rentable** dès lors qu'il y a **peu/pas d'intermission** (notre cas) **et** que la **formation est documentée**.
- Gain : **≈ +95 €/mois/intérimaire** sans formation, **≈ +353 €/mois** avec formation documentée.
- 👉 **Documenter la formation interne = 1er levier de marge** (récupération de la contribution FSPI ≈ 10 % du brut, sinon perdue). Nos formations internes existent mais ne sont pas référencées.

## Ce que l'outil prend en compte (spécificités intérim)
- IFM + CP (ratio SB/base 1,21 ou 1,10), séquestre vs mensualisation.
- RGDU 2026 (formule à exposant 1,75, Tmax 0,3981/0,4021) **et** réduction Fillon avant 2026, selon la date de prestation.
- **SMIC de référence gelé à 12,02 €/h** sur toute 2026 (la hausse de juin à 12,31 € n'est pas répercutée).
- Heures supp à ×1,00 dans le SMIC de référence (art. D. 241-7) ; déduction TEPA.
- **Deux seuils d'effectif distincts** : FNAL/RGDU à 50 salariés, TEPA à 20 — avec **gel Loi Pacte** (notre déduction HS 1,50 € gelée jusqu'au 31/12/2030).
- Majoration caisse CP intérim (×100/90) appliquée au coefficient, CTT seulement.
- Facturation hebdo vs paie mensuelle : consolidation par mois, détection auto du mois des bulletins.

## Corrections du 17/11/2026 (par ordre de dangerosité financière)
1. **Régularisation progressive RGDU** *(risque de redressement URSSAF)* — la marge Provision = Mensualisé ; la sur-déduction de cours de mois est isolée comme pure **trésorerie**, plus comptée comme marge.
2. **AT/MP par code risque client** — la cotisation accidents du travail est isolée et affichée par dossier (nos comptes : ~2,78 % en juin 2026 ; Transcan 3,00-3,02 % en 2024-2025).
3. **Taux IFM paramétrable** — 10 % par défaut, ajustable pour une branche à taux conventionnel différent.
4. **Semaines à cheval** — montant réparti au **prorata des jours calendaires** entre les deux mois (plus de bascule en bloc).

## Limites connues (à garder en tête)
- Taux de charges = part patronale lue sur le bulletin (bon proxy, pas une reconstitution cotisation par cotisation).
- Garantie d'intermission CDII = saisie manuelle (0 € chez nous).
- Avenants / acomptes / requalifications : hors périmètre.
- Dépendance au format des PDF BESTT/Pixid (un changement de gabarit peut casser la lecture).

## Validé sur 4 clients réels (juin 2026)
- **Poissonnerie Nice**, **Poissonnerie Puget**, **Deuerer**, **Transcan** — coefficients, AT/MP, couverture et marges recalculés.
- Rappels paramètres : Deuerer coef 1,85 ; Transcan coef **1,82** (depuis fin 2025) ; cf. [[Paramètres par client]].
- ⚠️ Transcan & Deuerer : factures de fin de mois (semaines 25-27) manquantes dans les lots → couverture partielle, marge affichée artificiellement basse tant qu'elles ne sont pas ajoutées.

Voir : [[Outils internes Talentis]] · [[Optimisations sociales]] · [[Règles de paie et facturation]] · [[Paramètres par client]]
Retour : [[Marges & Optimisations - Index]]
