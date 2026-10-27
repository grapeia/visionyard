"""Export JSON do Label Studio, com a mesma normalização do dump sqlite."""
import json
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

from .base import ImportReport, SourceSample
from .label_studio import _task_path
from .local import image_path
from .shapes import ls_results


class LabelStudioJSONImporter:
    kind = "label_studio_json"

    def __init__(self, json_path: str, image_root: str, image_base_uri: str = ""):
        self.path = Path(json_path)
        self.image_root = Path(image_root)
        self.image_base_uri = image_base_uri.rstrip("/")
        if not self.path.is_file():
            raise FileNotFoundError(json_path)

    def describe(self) -> str:
        return f"Label Studio (JSON) {self.path.name}"

    def classes(self) -> list[str]:
        nomes: set[str] = set()
        for sample in self.read(ImportReport()):
            nomes.update(f.cls_name for f in sample.boxes)
            nomes.update(f.cls_name for f in sample.polygons)
        return sorted(nomes)

    def read(self, report: ImportReport) -> Iterator[SourceSample]:
        try:
            tasks = json.loads(self.path.read_text())
            if not isinstance(tasks, list):
                raise ValueError("export deve conter uma lista de tarefas")
        except (OSError, UnicodeError, ValueError):
            report.skip(self.path.name, "export do Label Studio inválido")
            return
        for i, task in enumerate(tasks):
            key = f"task:{i}"
            try:
                rel = _task_path(json.dumps(task["data"]), preserve_absolute=True)
                if rel is None:
                    raise ValueError("não achei caminho de imagem")
                key = rel
                remoto = "://" in rel
                local_rel = (urllib.parse.urlparse(rel).path.lstrip("/")
                             if remoto else rel)
                if rel.startswith(("http://", "https://")):
                    local_rel = urllib.parse.unquote(local_rel)
                local = image_path(self.image_root, local_rel, report)
                relativo = local.relative_to(self.image_root.resolve()).as_posix()
                uri = (rel if "://" in rel else
                       f"{self.image_base_uri}/{relativo}"
                       if self.image_base_uri else relativo)
                sample = SourceSample(key=rel, uri=uri, local_path=str(local),
                                      metadata={"ls_task_id": task.get("id", i)})
                annotations = task.get("annotations", [])
                if not isinstance(annotations, list):
                    raise ValueError("annotations deve ser uma lista")
                ativas = [a for a in annotations if isinstance(a, dict)
                          and not a.get("was_cancelled")]
                if len(ativas) > 1:
                    report.warn(f"{key}: múltiplas anotações do LS serão combinadas; "
                                "confira objetos duplicados")
                for annotation in annotations:
                    if annotation.get("was_cancelled"):
                        continue
                    sample.annotated = True
                    ls_results(annotation.get("result"), sample, report)
                yield sample
            except (KeyError, TypeError, ValueError, AttributeError):
                report.skip(key, "tarefa inválida ou imagem fora/ausente em image_root")
