package com.example.roomfixture

import androidx.room.ColumnInfo
import androidx.room.Database
import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index
import androidx.room.PrimaryKey
import androidx.room.RoomDatabase

// Version 1: two entities, a unique index, a foreign key. The field and column
// names differ on purpose (name / full_name, userId / user_id), so a reader
// that confuses fieldPath with columnName is caught.
@Entity(tableName = "users", indices = [Index(value = ["email"], unique = true)])
data class User(
    @PrimaryKey(autoGenerate = true) val id: Long,
    @ColumnInfo(name = "full_name") val name: String,
    val email: String?,
)

@Entity(
    tableName = "notes",
    foreignKeys = [
        ForeignKey(
            entity = User::class,
            parentColumns = ["id"],
            childColumns = ["user_id"],
            onDelete = ForeignKey.CASCADE,
        ),
    ],
    indices = [Index("user_id")],
)
data class Note(
    @PrimaryKey val noteId: Long,
    @ColumnInfo(name = "user_id") val userId: Long,
    val body: String,
)

@Database(entities = [User::class, Note::class], version = 1)
abstract class AppDatabase : RoomDatabase()
