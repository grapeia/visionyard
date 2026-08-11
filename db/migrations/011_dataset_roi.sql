-- 011_dataset_roi.sql — região de interesse (ROI) por dataset.
--
-- Alguns projetos treinam para um único alvo que sempre ocupa a mesma faixa do
-- quadro (o item centralizado pelo disparo da câmera, por exemplo). Nesses casos há
-- vários objetos idênticos na cena e só um deve ser detectado: rotular só ele
-- deixa os outros como "fundo", e o detector recebe supervisão contraditória
-- ("este objeto = alvo, aquele idêntico = nada") — não aprende, mAP zera.
--
-- A ROI resolve por recorte: uma caixa normalizada [x,y,w,h] em frações 0–1 que
-- o pipeline aplica à imagem no treino, na avaliação e na inferência, de modo
-- idêntico nos três. Recortada a faixa central, sobra um alvo por imagem e a
-- supervisão volta a ser consistente.
--
-- Mora em duas camadas, seguindo o par dataset/dataset_version que já existe:
--
--   * `dataset.roi`          — config viva e editável (a fonte da UI).
--   * `dataset_version.roi`  — carimbo imutável no freeze: a versão registra a
--                              ROI com que foi congelada, e o modelo herda essa
--                              ROI pela linhagem model_version → training_run →
--                              dataset_version. Sem isso a inferência não saberia
--                              recortar igual ao treino e erraria em silêncio.
--
-- Formato do jsonb: {"x":..,"y":..,"w":..,"h":..} é a caixa canônica que o
-- engine usa; os campos opcionais {"anchor","w_pct","h_pct"} são dica da UI para
-- repopular os presets (âncora 3×3 + largura%/altura%). NULL = sem recorte.

BEGIN;

ALTER TABLE dataset ADD COLUMN roi jsonb;
ALTER TABLE dataset_version ADD COLUMN roi jsonb;

COMMIT;
