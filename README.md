<p align="center">
  <img src="docs/assets/visionyard-logo.svg" alt="Visionyard — da imagem ao modelo" width="640">
</p>

<p align="center">
  Organize imagens, anote, treine e acompanhe seus modelos de visão computacional em um só lugar.
  <br>
  <strong>Você controla os dados. Suas máquinas executam os treinos.</strong>
</p>

## Da imagem ao modelo

```mermaid
flowchart LR
    A[Imagens] --> B[Anotação]
    B --> C[Dataset versionado]
    C --> D[Treino na sua GPU]
    D --> E[Avaliação do modelo]
    E --> F[Publicação e deploy]

    classDef etapa fill:#131a21,stroke:#4a5c6b,color:#e6edf3
    classDef destaque fill:#ff8a1f,stroke:#ff8a1f,color:#0d1217
    class A,B,C,E,F etapa
    class D destaque
```

## O que você pode fazer

| | No Visionyard |
|---|---|
| **Organizar e anotar** | Reúna imagens e mantenha o histórico das anotações de cada pessoa. |
| **Versionar datasets** | Preserve os dados usados em cada treino para comparar resultados. |
| **Treinar do seu jeito** | Escolha a máquina e ajuste os hiperparâmetros em um painel separado, com importação e reutilização de configurações. |
| **Acompanhar resultados** | Veja o progresso, as métricas e as versões dos modelos pela interface. |
| **Colocar em operação** | Exporte modelos para ONNX ou TensorRT e gerencie a publicação e o deploy. |

## Comece por aqui

- **Instalar a plataforma:** [ambiente local](docs/guia-tecnico.md#subir-desenvolvimento) ou [produção](docs/guia-tecnico.md#subir-produção).
- **Conectar suas máquinas:** [guia do agente](agent/README.md).
- **Personalizar o treinamento:** [configurações e presets](docs/guia-tecnico.md#personalizar-um-treino).
- **Entender a parte técnica:** [arquitetura, configuração e testes](docs/guia-tecnico.md).
