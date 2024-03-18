function confirm_ready(answer){
    socket.emit("message_command",
        {
            "command": {
                "event": "confirm_ready",
                "answer": answer
            },
            "room": self_room
        }
    )
}

$(document).ready(() => {
    socket.on("command", (data) => {
        if (typeof data.command === "object" && data.command.event === "survey") {
            $("#instr").html(data.command.survey);

            $("#survey_button").click(() => {
                if (validateForm()) {
                    const answers = get_answers();
                    socket.emit("message_command", {
                        command: {
                            event: "submit_survey",
                            answers: answers
                        },
                        room: self_room
                    });
                }
            });
        }
    });
});
