from console.vocab import (
    head_alphabet,
    read_alphabet,
    spellings,
    split_representable,
    vocab_path,
)

# Shape of the real `v3_vocab.txt`: the word boundary, the 32 lowercase Cyrillic
# letters the head can write (no `ё`), then the decoder's blank symbol.
CYRILLIC = "абвгдежзийклмнопрстуфхцчшщъыьэюя"
LATIN = "abcdefghijklmnopqrstuvwxyz"


def write_vocab(path, tokens):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{token} {index}\n" for index, token in enumerate(tokens)),
        encoding="utf-8",
    )
    return path


def cyrillic_vocab(path):
    return write_vocab(path, ["▁", *CYRILLIC, "<blk>"])


def multilingual_vocab(path):
    return write_vocab(path, ["▁", *CYRILLIC, *LATIN, "<blk>", "<unk>"])


def cased_bpe_vocab(path):
    """Shape of the real `v3_e2e_rnnt_vocab.txt`: word pieces over 147 characters,
    capitals, Latin, digits and punctuation included."""
    pieces = ["▁на", "▁по", "ли", "Пё", "GPT", "₽"]
    return write_vocab(
        path,
        ["▁", *CYRILLIC, CYRILLIC.upper(), *LATIN, LATIN.upper(), "0123456789", *pieces, "<blk>"],
    )


def test_charset_is_the_lowercase_letters_the_word_boundary_and_a_space(tmp_path):
    alphabet = read_alphabet(cyrillic_vocab(tmp_path / "v3_vocab.txt"))
    assert alphabet is not None
    assert alphabet.chars == frozenset(CYRILLIC) | {"▁", " "}
    assert len(alphabet.chars) == 34  # the number the engine's own count matches


def test_control_tokens_do_not_lend_their_letters_to_the_head(tmp_path):
    alphabet = read_alphabet(cyrillic_vocab(tmp_path / "v3_vocab.txt"))
    assert alphabet is not None
    assert not alphabet.chars & set("<>blk")


def test_character_vocabulary_is_not_marked_approximate(tmp_path):
    alphabet = read_alphabet(cyrillic_vocab(tmp_path / "v3_vocab.txt"))
    assert alphabet is not None and alphabet.subword is False


def test_word_piece_vocabulary_is_marked_approximate(tmp_path):
    path = write_vocab(tmp_path / "v3_e2e_rnnt_vocab.txt", ["▁", "а", "▁при", "вет", "<blk>"])
    alphabet = read_alphabet(path)
    assert alphabet is not None and alphabet.subword is True


def test_missing_vocabulary_file_means_we_know_nothing(tmp_path):
    assert read_alphabet(tmp_path / "nope.txt") is None


def test_broken_vocabulary_file_means_we_know_nothing(tmp_path):
    path = tmp_path / "v3_vocab.txt"
    path.write_bytes(b"fake-weights")
    assert read_alphabet(path) is None

    path.write_bytes(b"\x00\x01\x02\xff\xfe")
    assert read_alphabet(path) is None

    path.write_text("", encoding="utf-8")
    assert read_alphabet(path) is None


def test_oversized_file_is_not_treated_as_a_vocabulary(tmp_path, monkeypatch):
    monkeypatch.setattr("console.vocab.MAX_VOCAB_BYTES", 8)
    assert read_alphabet(cyrillic_vocab(tmp_path / "v3_vocab.txt")) is None


def test_the_engine_tries_three_spellings_in_this_order():
    """Mirrors `encode_phrase` upstream: as typed, lowercased, then `ё` folded to `е`."""
    assert spellings("Пётр") == ("Пётр", "пётр", "петр")


def test_yo_is_folded_because_the_head_writes_e_in_its_place(tmp_path):
    alphabet = read_alphabet(cyrillic_vocab(tmp_path / "v3_vocab.txt"))
    usable, dropped = split_representable(["аймоп", "пётр иванович сидоров"], alphabet)
    assert usable == ["аймоп", "пётр иванович сидоров"]
    assert dropped == []


def test_capitalised_phrase_survives_because_the_engine_lowercases_it(tmp_path):
    """Every shipped vocabulary is lowercase, so brand names would otherwise all die."""
    alphabet = read_alphabet(cyrillic_vocab(tmp_path / "v3_vocab.txt"))
    usable, dropped = split_representable(["АйМоп", "аймоп"], alphabet)
    assert usable == ["АйМоп", "аймоп"] and dropped == []


def test_multiword_phrase_survives_because_space_is_writable(tmp_path):
    alphabet = read_alphabet(cyrillic_vocab(tmp_path / "v3_vocab.txt"))
    usable, dropped = split_representable(["ай ти отдел"], alphabet)
    assert usable == ["ай ти отдел"] and dropped == []


def test_latin_phrase_dropped_on_russian_head_but_kept_on_multilingual(tmp_path):
    russian = read_alphabet(cyrillic_vocab(tmp_path / "v3_vocab.txt"))
    multilingual = read_alphabet(multilingual_vocab(tmp_path / "multilingual_vocab.txt"))
    phrases = ["python", "аймоп"]
    assert split_representable(phrases, russian) == (["аймоп"], ["python"])
    assert split_representable(phrases, multilingual) == (phrases, [])


def test_only_a_missing_alphabet_drops_a_phrase_now(tmp_path):
    """Case is never the reason: what kills a phrase is a script the head cannot write."""
    multilingual = read_alphabet(multilingual_vocab(tmp_path / "multilingual_vocab.txt"))
    russian = read_alphabet(cyrillic_vocab(tmp_path / "v3_vocab.txt"))
    phrases = ["OpenWhispr", "ChatGPT", "питон"]
    assert split_representable(phrases, multilingual) == (phrases, [])
    assert split_representable(phrases, russian) == (["питон"], ["OpenWhispr", "ChatGPT"])


def test_empty_phrase_list_splits_into_nothing(tmp_path):
    alphabet = read_alphabet(cyrillic_vocab(tmp_path / "v3_vocab.txt"))
    assert split_representable([], alphabet) == ([], [])


def test_vocabulary_path_comes_from_the_catalog(tmp_path):
    assert vocab_path("rnnt", tmp_path) == tmp_path / "v3_vocab.txt"
    assert vocab_path("e2e_rnnt", tmp_path) == tmp_path / "v3_e2e_rnnt_vocab.txt"
    assert vocab_path("ml_ctc", tmp_path) == tmp_path / "multilingual_vocab.txt"
    assert vocab_path("ml_ctc_large", tmp_path) == tmp_path / "multilingual_vocab.txt"


def test_unknown_head_has_no_vocabulary_and_no_alphabet(tmp_path):
    assert vocab_path("whisper-large", tmp_path) is None
    assert head_alphabet("whisper-large", tmp_path) is None


def test_head_alphabet_reads_the_file_of_that_head(tmp_path):
    cyrillic_vocab(tmp_path / "v3_vocab.txt")
    assert head_alphabet("rnnt", tmp_path) is not None
    assert head_alphabet("ml_ctc", tmp_path) is None  # its file is not there yet


def test_character_vocabularies_write_neither_capitals_nor_digits(tmp_path):
    russian = read_alphabet(cyrillic_vocab(tmp_path / "v3_vocab.txt"))
    multilingual = read_alphabet(multilingual_vocab(tmp_path / "multilingual_vocab.txt"))
    assert (russian.writes_latin, russian.writes_digits) == (False, False)
    assert (multilingual.writes_latin, multilingual.writes_digits) == (True, False)


def test_cased_bpe_vocabulary_writes_latin_and_digits(tmp_path):
    """`e2e_rnnt` keeps `ChatGPT` as typed, which is why the cascade starts there."""
    alphabet = read_alphabet(cased_bpe_vocab(tmp_path / "v3_e2e_rnnt_vocab.txt"))
    assert alphabet.subword is True
    assert (alphabet.writes_latin, alphabet.writes_digits) == (True, True)
    usable, dropped = split_representable(["OpenWhispr", "ChatGPT"], alphabet)
    assert (usable, dropped) == (["OpenWhispr", "ChatGPT"], [])


# ----------------------------------------------------------------- the endpoint


async def test_glossary_endpoint_reports_reach_next_to_the_old_fields(client_ready):
    settings = client_ready.app.state.settings
    cyrillic_vocab(settings.model_dir / "v3_vocab.txt")
    await client_ready.post("/api/glossary", json={"text": "аймоп, OpenWhispr, ай ти отдел"})

    body = (await client_ready.get("/api/glossary")).json()
    assert body["text"] == "аймоп\nOpenWhispr\nай ти отдел"
    assert body["count"] == 3
    assert body["boost"] == 5.0
    assert body["variant"] == "rnnt"
    assert body["usable_count"] == 2
    assert body["dropped"] == ["OpenWhispr"]
    assert body["approximate"] is False


async def test_glossary_endpoint_claims_nothing_when_the_vocabulary_is_broken(client_ready):
    # The fake engine "downloads" weights as the text `fake-weights`.
    await client_ready.post("/api/glossary", json={"text": "аймоп, OpenWhispr"})
    body = (await client_ready.get("/api/glossary")).json()
    assert body["count"] == 2
    assert body["usable_count"] is None
    assert body["dropped"] == []
    assert body["approximate"] is False


async def test_glossary_endpoint_survives_a_model_directory_that_does_not_exist(client_stopped):
    await client_stopped.post("/api/glossary", json={"text": "аймоп, OpenWhispr"})
    response = await client_stopped.get("/api/glossary")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["variant"] == "rnnt"  # nothing deployed yet: the preselected head
    assert body["usable_count"] is None


async def test_glossary_endpoint_follows_the_deployed_head(client_ready):
    from console.state import EngineConfig

    settings = client_ready.app.state.settings
    await client_ready.post("/api/glossary", json={"text": "python, аймоп"})
    cyrillic_vocab(settings.model_dir / "v3_vocab.txt")
    body = (await client_ready.get("/api/glossary")).json()
    assert body["variant"] == "rnnt" and body["dropped"] == ["python"]

    await client_ready.app.state.supervisor.deploy(EngineConfig(variant="ml_ctc"))
    multilingual_vocab(settings.model_dir / "multilingual_vocab.txt")
    body = (await client_ready.get("/api/glossary")).json()
    assert body["variant"] == "ml_ctc"
    assert body["usable_count"] == 2 and body["dropped"] == []


async def test_glossary_endpoint_reports_what_the_head_can_spell(client_ready):
    settings = client_ready.app.state.settings
    await client_ready.post("/api/glossary", json={"text": "аймоп"})

    cyrillic_vocab(settings.model_dir / "v3_vocab.txt")
    body = (await client_ready.get("/api/glossary")).json()
    assert body["alphabet"] == {"latin": False, "digits": False}

    (settings.model_dir / "v3_vocab.txt").unlink()
    body = (await client_ready.get("/api/glossary")).json()
    assert body["alphabet"] is None  # no weights, no claim
