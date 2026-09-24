# Antirez, con calma

Un archivio personale e locale dei video di [Salvatore Sanfilippo](https://www.youtube.com/@antirez/videos): trascrizioni, riassunti in italiano, ricerca e una chat con riferimenti ai video originali.

## Per partire

Richiede Python 3.11+, `yt-dlp` e `ffmpeg` nel PATH. Su questo PC gli ultimi due sono già installati.

1. Da questa cartella avvia `python3 -m antirez serve`.
2. Apri [http://127.0.0.1:8765](http://127.0.0.1:8765), vai su **Impostazioni** e inserisci la chiave di [Google AI Studio](https://aistudio.google.com/apikey). Puoi modificare lì anche i modelli di trascrizione, chat e ricerca: le modifiche valgono subito. La chiave viene salvata solo nel file locale `.env` (permessi riservati) e non viene mostrata di nuovo dalla pagina.
3. Premi **Controlla nuovi video**. La prima esecuzione elabora gli ultimi 10; può richiedere tempo e chiamate API per ogni video. La pagina mostra i progressi.

Puoi anche acquisire i video dal terminale con `python3 -m antirez sync` e vedere lo stato con `python3 -m antirez status`. Il server accetta richieste solo da questo PC (`127.0.0.1`). Non serve un account oltre alla chiave Gemini.

### Controllo automatico

Dopo avere impostato la chiave, esegui **una volta** `python3 -m antirez install-timer`. Crea un timer `systemd --user` che controlla il canale alle 09, 15 e 21 (con qualche minuto di ritardo casuale) e recupera un controllo perso mentre il PC era spento. Avvia anche la pagina locale automaticamente a ogni accesso al PC: puoi aprire [http://127.0.0.1:8765](http://127.0.0.1:8765) quando vuoi. Se il PC resta spento per settimane, oltre ai 15 video del feed, il programma cerca i successivi nella pagina del canale. Per disattivare i servizi: `systemctl --user disable --now antirez-sync.timer antirez-web.service`.

L'acquisizione programmata funziona anche senza la pagina aperta, quando la sessione utente del PC è attiva. Per controllare gli esiti del timer: `journalctl --user -u antirez-sync.service -n 50`.

### Pubblicarlo su un server

Il `Dockerfile` avvia la stessa pagina in un container (porta 8765), con archivio e `.env` nel volume `/app/data`. Fuori da questo PC servono tre variabili: `ALLOWED_HOSTS` con il dominio pubblico, `AUTH_PASSWORD` per chiedere una password a ogni visita (utente `antirez`, cambiabile con `AUTH_USER`) e, se preferisci, `GEMINI_API_KEY`. Per un amico aggiungi `GUEST_ACCOUNTS="Nome:password"` (più account separati da virgole): vede archivio e chat con conversazioni sue, non le Impostazioni né «Controlla nuovi video», e può fare al massimo `GUEST_CHAT_LIMIT` domande al giorno (30 se non lo imposti; il contatore riparte a mezzanotte, ora italiana). Il timer `systemd` non esiste nel container: pianifica `python -m antirez sync` con lo strumento del server (per esempio le Scheduled Tasks di Coolify). Da un IP di datacenter YouTube di solito blocca il download dell'audio, quindi la trascrizione passa a Gemini dal link del video.

## Come funziona

- Feed YouTube per scoprire i nuovi video; `yt-dlp` e `ffmpeg` per estrarre l'audio in segmenti di 12 minuti, adatti alla richiesta inline.
- `gemini-3.5-transcribe` su API `v1alpha` in modalità `SMART`, come in Maledetti Vocali. Nessuna lingua imposta: i video possono essere in inglese. La modalità SMART ripulisce esitazioni, quindi il testo non è una trascrizione verbatim certificata e non fornisce timestamp. Su questa connessione YouTube può bloccare il download dell'audio (HTTP 403): in quel caso Gemini Flash trascrive il video dal suo link in segmenti di 5 minuti. Se anche questa richiesta fallisce o tronca un video lungo, si usano i sottotitoli originali di YouTube quando disponibili. La pagina segnala sempre quale fonte ha prodotto il testo.
- `gemini-3.5-flash` per i riassunti in italiano e per la chat. Ogni risposta della chat include i passaggi e i link ai video usati come fonti.
- SQLite locale in `data/archive.sqlite3` per video, conversazioni e ricerca testuale FTS5. Gli embedding di `gemini-embedding-001` affinano la ricerca concettuale quando disponibili; se falliscono resta la ricerca testuale.
- Un video già acquisito non viene elaborato due volte. Dopo un'interruzione, la trascrizione completata viene riutilizzata per ritentare riassunto o indicizzazione. La chiave resta in `.env` (ignorato da Git); puoi anche configurare `.env` a mano copiando `.env.example`. Le variabili d'ambiente del sistema hanno la precedenza e si gestiscono fuori dalla pagina. Gli audio temporanei vengono rimossi a fine elaborazione.

Le chiamate Gemini possono consumare quota o generare costi secondo il tuo piano Google AI Studio: il primo giro di 10 video richiede più chiamate delle esecuzioni successive. Se una trascrizione non è riuscita, il video resta visibile con l'errore e il successivo controllo ritenta.

## Verifica

`python3 -m unittest discover -s tests -v`
