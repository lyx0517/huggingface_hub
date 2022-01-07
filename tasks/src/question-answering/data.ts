import type { TaskData } from "../Types";

import { PipelineType } from "../../../widgets/src/lib/interfaces/Types";
import { TASKS_MODEL_LIBRARIES } from "../const";
import { answers } from "../../../../views/components/Question/stores";

const taskData: TaskData = {
	datasets: [
		{
			// TODO write proper description
			description: "A famous question answering dataset based on English articles from Wikipedia.",
			id:          "squad_v2",
		},
		{
			// TODO write proper description
			description: "A dataset of aggregated anonymized actual queries issued to the Google search engine.",
			id:          "natural_questions",
		},
	],
	demo: {
		inputs: [
			{
				label:   "Question",
				content: "What name is also used to describe the Amazon rainforest in English?",
				type: "text",
			},
			{
				label:   "Context",
				content: "The Amazon rainforest, also known in English as Amazonia or the Amazon Jungle",
				type: "text",
			},
		],
		outputs: [
			{
				label:   "Answer",
				content: "Amazonia",
				type:    "text",
			},
		],
	},
	id:        "question-answering",
	label:     PipelineType["question-answering"],
	libraries: TASKS_MODEL_LIBRARIES["question-answering"],
	metrics:   [
		{
			description: "The Exact Match metric is based on the strict character match of the predicted answer and the right answer. For correct predicted answers, the Exact Match will be 1. Even if only one character is different, the Exact Match will be 0.",
			id:          "exact-match",
		},
		{
			description: " The F1-Score metric is useful if we value both false positives and false negatives equally. The F1-Score is calculated on each word in the predicted sequence against the correct answer.",
			id:          "f1",
		},
	],
	models: [
		{
			description: "A robust baseline model for most question-answering domains.",
			id:          "deepset/roberta-base-squad2",
		},
		{
			description: "A special model that can answer questions from tables!",
			id:          "google/tapas-base-finetuned-wtq",
		},
	],
	summary:      "Question Answering models can retrieve the answer to a question from a given text, which is useful for searching for an answer in a document. Some question answering models can generate answers without context!",
	widgetModels: ["deepset/roberta-base-squad2"],
	youtubeId:    "ajPx5LwJD-I",
};


export default taskData;
