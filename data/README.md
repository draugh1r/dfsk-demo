# Comuni italiani

`comuni_italiani.csv` è una fotografia dell'[elenco ufficiale ISTAT](https://www.istat.it/classificazione/codici-dei-comuni-delle-province-e-delle-regioni/) aggiornato al 21 febbraio 2026. Contiene comune, sigla e nome della provincia. Il chatbot lo legge localmente e non effettua chiamate geografiche durante le conversazioni.

Per aggiornare l'elenco quando ISTAT pubblica variazioni amministrative, eseguire `python scripts/update_comuni.py` in un ambiente con le dipendenze del progetto, verificare la differenza e includere il CSV aggiornato nel deploy. L'aggiornamento non modifica il database dei concessionari.
